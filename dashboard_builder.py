"""Turn a Qlik app's data model into a dashboard, via a local LLM.

The flow is: read the real field list -> ask the model for a spec -> check
every visualization in that spec against the real fields -> build only what
passes. The checking step is the important one. Qlik does not reject a chart
that references a field which doesn't exist; it creates the object happily
and renders an empty box, so a hallucinated field name turns into a silent
blank chart rather than an error. Everything here exists to catch that
before it reaches the engine.
"""

import logging
import re

from chart_specs import (
    CHART_TYPES,
    DIMENSIONAL_TYPES,
    chart_requirements,
    resolve_chart_type,
    resolve_colour,
)
from config import APP_NAME, OLLAMA_MODEL
from ollama_client import OllamaClient, OllamaError
from prompts import DASHBOARD_SYSTEM_PROMPT, build_dashboard_prompt
from qlik_engine import QlikEngine, QlikEngineError, bare_field_name

log = logging.getLogger(__name__)


# Only simple single-field aggregations are allowed. Anything else - SortBy,
# Limit, Aggr, Rank, nested expressions - is a known source of blank charts:
# the object is created, then silently fails to calculate.
VALID_EXPRESSION_RE = re.compile(
    r"^(Sum|Count|Avg|Min|Max)\(\s*(DISTINCT\s+)?\[[^\[\]]+\]\s*\)$",
    re.IGNORECASE,
)

_BRACKETED_FIELD_RE = re.compile(r"\[([^\[\]]+)\]")


def expression_field(expression):
    """The bracketed field name inside a simple aggregation, or None."""
    match = _BRACKETED_FIELD_RE.search(expression or "")
    return match.group(1) if match else None


def effective_expression(viz):
    """The expression that will actually reach Qlik for this visualization.

    When the model omits `measure_expression`, QlikEngine.create_chart falls
    back to Sum([measure]). Validation has to check that fallback, not shrug
    and pass the spec through - that gap is exactly how a KPI with a
    hallucinated measure used to slip past unchecked.
    """
    expression = (viz.get("measure_expression") or "").strip()
    if expression:
        return expression

    measure = bare_field_name(viz.get("measure"))
    return f"Sum([{measure}])" if measure else ""


def normalize_visualization(viz):
    """Return a cleaned copy of one visualization spec.

    The prompt says dimensions are bare field names and only expressions use
    brackets. Models mix that up constantly, and "[Customer Segment]" passed
    through as a dimension becomes a literal field name containing brackets
    that matches nothing.
    """

    clean = dict(viz)
    # "scatter", "pivot table", "bar chart" all name real types.
    clean["type"] = resolve_chart_type(clean.get("type")) or (
        clean.get("type") or ""
    ).strip().lower()
    clean["title"] = (clean.get("title") or "").strip()
    clean["dimension"] = bare_field_name(clean.get("dimension"))
    clean["measure"] = bare_field_name(clean.get("measure"))
    clean["measure_expression"] = (clean.get("measure_expression") or "").strip()

    # "top 5" is a dimension limit, not something the expression can do.
    try:
        limit = int(clean.get("limit") or 0)
    except (TypeError, ValueError):
        limit = 0
    clean["limit"] = limit if 0 < limit <= 100 else None

    # Unrecognised colour names are dropped rather than rejected: a chart is
    # still worth building if the model asked for "vermilion".
    clean["color"] = resolve_colour(clean.get("color"))

    # A KPI is a single aggregate with no grouping. Models often attach a
    # dimension anyway, which would turn it into a one-column table.
    if clean["type"] == "kpi":
        clean["dimension"] = None

    return clean


# A dimension is a set of categories a human reads off an axis or a legend.
# Above roughly this many distinct values it stops being one: the chart turns
# into an unreadable smear, and a table turns into tens of thousands of rows.
# The prompt asks the model to respect cardinality; this enforces it, because
# "Sales by Order Id" over 65,752 ids is a chart that technically renders and
# tells you nothing.
MAX_DIMENSION_CARDINALITY = 200


def validate_visualization(viz, field_names=None, field_info=None, engine=None):
    """Check one normalized visualization. Returns an error string, or None.

    field_names: the app's real field names. When None the field-existence
    checks are skipped - only shape and expression syntax are enforced.
    field_info: optional {name: field dict} carrying cardinality, used to
    reject dimensions that are really identifiers.
    """

    viz_type = viz.get("type")
    if viz_type not in CHART_TYPES:
        return f"unknown chart type {viz_type!r} (expected one of {', '.join(CHART_TYPES)})"

    if not viz.get("title"):
        return "missing title"

    dimension = viz.get("dimension")

    # Each type declares what it can draw, taken from Qlik's own bundle.
    (min_dims, _max_dims), (min_meas, max_meas) = chart_requirements(viz_type)

    # Qlik permits a bar chart with no dimension - it draws one bar. Allowed
    # is not the same as useful, so the classic types still require one on
    # top of whatever the bundle says.
    if (min_dims or viz_type in DIMENSIONAL_TYPES) and not dimension:
        return f"{viz_type} needs a dimension to group by"

    if min_meas > 1:
        # A scatter plot needs two measures and this spec carries one, so it
        # would build something that cannot draw. Better to say so than to
        # produce an empty chart.
        return (
            f"{viz_type} needs {min_meas} measures and a dashboard spec "
            "provides one - use create_chart for it"
        )

    if dimension and field_names is not None and dimension not in field_names:
        return f"dimension {dimension!r} is not a field in this app"

    if dimension and field_info:
        cardinality = (field_info.get(dimension) or {}).get("cardinality")
        if cardinality and cardinality > MAX_DIMENSION_CARDINALITY:
            return (
                f"dimension {dimension!r} has {cardinality:,} distinct values - "
                f"too many to group by (limit {MAX_DIMENSION_CARDINALITY:,}); "
                "it's an identifier, not a category"
            )
        if cardinality == 1:
            # One bar, always. Nothing to compare, so nothing to see.
            return (
                f"dimension {dimension!r} has the same value in every row, so "
                "grouping by it produces a single bar"
            )

    expression = effective_expression(viz)
    if not expression:
        return "no measure or measure_expression"

    if engine is not None:
        # The engine judges the expression itself, so set analysis, Aggr and
        # nested aggregations are all allowed - it also reports invented
        # field names, which are valid syntax and still produce an empty
        # chart. Restricting to one simple aggregation was only ever a
        # stand-in for having no validator.
        check = engine.check_expression(expression)
        if not check["valid"]:
            detail = check["error"] or (
                "unknown field(s): " + ", ".join(check["bad_fields"])
            )
            return f"measure_expression {expression!r} was rejected by Qlik: {detail}"
        return None

    # No engine to ask (unit tests, offline checks): fall back to the
    # conservative shape.
    if not VALID_EXPRESSION_RE.match(expression):
        return (
            f"measure_expression {expression!r} isn't a simple aggregation "
            "(Sum/Count/Avg/Min/Max of one field)"
        )

    field = expression_field(expression)
    if field and field_names is not None and field not in field_names:
        return f"measure_expression references unknown field {field!r}"

    return None


# Charts are placed in the order they are built, so the order decides the
# layout. KPIs are small and belong across the top; a table is full width and
# belongs at the bottom. Left in model order you get a headline number
# stranded between two bar charts.
_LAYOUT_ORDER = {"kpi": 0, "barchart": 1, "linechart": 1, "piechart": 2, "table": 3}


def arrange(visualizations):
    """Order visualizations so the sheet reads top-down: KPIs, charts, table.

    Stable, so the model's own ordering still decides ties.
    """
    return sorted(
        visualizations,
        key=lambda v: _LAYOUT_ORDER.get((v.get("type") or "").strip().lower(), 2),
    )


def validate_spec(spec):
    """Check the top-level shape of a model-produced dashboard spec."""

    if not isinstance(spec, dict):
        raise ValueError(f"dashboard spec must be an object, got {type(spec).__name__}")

    title = spec.get("dashboard_title")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("dashboard spec is missing a non-empty 'dashboard_title'")

    visualizations = spec.get("visualizations")
    if not isinstance(visualizations, list) or not visualizations:
        raise ValueError("dashboard spec is missing a non-empty 'visualizations' list")

    if not all(isinstance(v, dict) for v in visualizations):
        raise ValueError("every entry in 'visualizations' must be an object")

    return spec


# ----------------------------------------------------------------------


def enrich_fields(engine, fields, max_cardinality=100, sample_size=5, limit=30):
    """Attach real sample values to the plausible dimension candidates.

    Field names alone are ambiguous - "Type" could be a payment type or a
    shipping type, and a model guessing wrong produces a chart with a title
    that doesn't match its own data. Only low-cardinality fields are profiled:
    they're the ones that can serve as dimensions, and each profile is a
    round trip to the engine.
    """

    candidates = [
        f for f in fields
        if f.get("cardinality") and 2 <= f["cardinality"] <= max_cardinality
    ][:limit]

    for field in candidates:
        try:
            profile = engine.profile_field(field["name"], sample_size=sample_size)
        except QlikEngineError as e:
            log.debug("Could not profile %r: %s", field["name"], e)
            continue
        if profile.get("samples"):
            field["samples"] = profile["samples"]

    log.info("Profiled %d candidate dimension(s)", len(candidates))
    return fields


def design_dashboard(fields, model=OLLAMA_MODEL, instruction=None, client=None):
    """Ask the LLM to turn a field list into a dashboard spec (dict).

    instruction: optional free-text request steering what gets designed.
    client: injectable for testing; defaults to a real OllamaClient.
    """

    client = client or OllamaClient(model)
    prompt = build_dashboard_prompt(fields, instruction=instruction)
    spec = client.ask_json(prompt, system=DASHBOARD_SYSTEM_PROMPT)

    return validate_spec(spec)


def build_dashboard(engine, spec, field_names=None, fields=None, validate_with_engine=True):
    """Create the sheet and charts in Qlik from a dashboard spec.

    field_names: the app's real field names, used to reject visualizations
    that reference fields the model invented. Strongly recommended - without
    it, bad references reach Qlik and become silently blank charts.
    fields: the full field list, so cardinality can also be enforced.

    Returns (built, skipped) where built is a list of normalized specs and
    skipped is a list of (spec, reason) pairs.
    """

    validate_spec(spec)

    field_info = {f["name"]: f for f in fields} if fields else None
    if field_names is None and fields:
        field_names = set(field_info)

    validator = engine if (
        validate_with_engine and hasattr(engine, "check_expression")
    ) else None

    title = spec["dashboard_title"].strip()
    visualizations = arrange(spec["visualizations"])
    log.info("Creating sheet %r", title)
    engine.create_sheet(title)

    built = []
    skipped = []

    seen = set()

    for raw_viz in visualizations:
        viz = normalize_visualization(raw_viz)

        error = validate_visualization(
            viz, field_names, field_info=field_info, engine=validator
        )
        if error:
            skipped.append((viz, error))
            log.warning("Skipped %r: %s", viz.get("title") or "(untitled)", error)
            continue

        # Models happily produce "Sales by Region" as both a bar chart and a
        # pie chart, or the same chart twice under different titles. Three
        # views of one number is not a dashboard.
        signature = (viz["type"], viz.get("dimension"), effective_expression(viz))
        if signature in seen:
            skipped.append((viz, "duplicate of a chart already on this sheet"))
            log.warning("Skipped %r: duplicate", viz.get("title"))
            continue
        seen.add(signature)

        try:
            engine.create_chart(
                viz["type"],
                viz["title"],
                dimension=viz.get("dimension"),
                measure=viz.get("measure"),
                measure_expression=viz.get("measure_expression") or None,
                limit=viz.get("limit") or None,
                colour=viz.get("color") or None,
            )
        except QlikEngineError as e:
            skipped.append((viz, str(e)))
            log.warning("Skipped %r: %s", viz.get("title"), e)
            continue

        built.append(viz)
        log.info("Built %s %r", viz["type"], viz["title"])

    return built, skipped


def _partition(visualizations, field_names, field_info, seen=None, engine=None):
    """Split specs into (buildable, [(spec, reason), ...]), dropping repeats."""
    seen = seen if seen is not None else set()
    good, bad = [], []

    for raw in visualizations:
        viz = normalize_visualization(raw)
        error = validate_visualization(
            viz, field_names, field_info=field_info, engine=engine
        )
        if error:
            bad.append((viz, error))
            continue

        signature = (viz["type"], viz.get("dimension"), effective_expression(viz))
        if signature in seen:
            bad.append((viz, "duplicate of a chart already on this sheet"))
            continue

        seen.add(signature)
        good.append(viz)

    return good, bad


def design_full_dashboard(fields, model=OLLAMA_MODEL, instruction=None, client=None,
                          retries=1, engine=None):
    """Design a dashboard, and top it up when charts get rejected.

    A model asked for six charts routinely produces one that groups by a
    constant field or an identifier. Those are dropped for good reason, but
    dropping them silently is how "make me six" quietly becomes five - so
    the shortfall is requested again, telling the model exactly what was
    rejected and why.
    """

    field_names = {f["name"] for f in fields}
    field_info = {f["name"]: f for f in fields}

    spec = design_dashboard(fields, model=model, instruction=instruction, client=client)
    wanted = len(spec["visualizations"])

    seen = set()
    good, bad = _partition(
        spec["visualizations"], field_names, field_info, seen, engine=engine
    )

    for _ in range(max(0, retries)):
        missing = wanted - len(good)
        if missing <= 0 or not bad:
            break

        rejected = "; ".join(
            f"{v.get('title') or 'untitled'} ({reason})" for v, reason in bad[-4:]
        )
        top_up = (
            f"{instruction or 'Design a dashboard.'}\n\n"
            f"Design exactly {missing} more visualization(s) for this same "
            f"dashboard. These were rejected, so do not repeat them or reuse "
            f"the fields that caused them: {rejected}. Do not repeat any of "
            f"these either: "
            + "; ".join(f"{v['type']} of {v.get('dimension')}" for v in good)
        )

        try:
            extra = design_dashboard(fields, model=model, instruction=top_up, client=client)
        except (ValueError, OllamaError) as e:
            log.warning("Could not top up the dashboard: %s", e)
            break

        more_good, more_bad = _partition(
            extra["visualizations"], field_names, field_info, seen, engine=engine
        )
        if not more_good:
            bad.extend(more_bad)
            break

        good.extend(more_good[:missing])
        bad = more_bad

    # Anything still unbuildable is passed through so build_dashboard reports
    # it as skipped rather than it disappearing without explanation.
    spec["visualizations"] = good + [v for v, _ in bad]
    return spec


def build_sheet(engine, title, charts, fields=None, description="Created by AI"):
    """Create one sheet with all its charts in a single call.

    Building a sheet chart-by-chart means a caller can leave a half-finished
    sheet behind when one chart is rejected. Taking the whole sheet at once
    lets everything be validated up front and reported together.

    Returns {sheet, sheet_id, built, skipped}.
    """

    spec = {
        "dashboard_title": title,
        "visualizations": charts if isinstance(charts, list) else [],
    }

    if not spec["visualizations"]:
        raise ValueError("build_sheet needs at least one chart")

    built, skipped = build_dashboard(engine, spec, fields=fields)

    return {
        "sheet": title,
        "sheet_id": engine.sheet_id,
        "built": [{"type": v["type"], "title": v["title"]} for v in built],
        "skipped": [{"title": v.get("title"), "reason": r} for v, r in skipped],
    }


def run(app_name=APP_NAME, model=OLLAMA_MODEL, instruction=None):
    """End-to-end: connect, inspect, design, build, save.

    Returns (spec, built, skipped).
    """

    with QlikEngine() as engine:
        log.info("Opening app %r", app_name)
        engine.open_app(app_name)

        log.info("Reading the data model")
        fields = engine.get_fields()
        if not fields:
            raise QlikEngineError(
                f"No fields found in app {app_name!r}. Is data loaded into it?"
            )

        log.info("Found %d fields. Sampling real values", len(fields))
        enrich_fields(engine, fields)

        log.info("Asking %s to design a dashboard", model)
        spec = design_dashboard(fields, model=model, instruction=instruction)

        built, skipped = build_dashboard(engine, spec, fields=fields)

        if not built:
            log.warning("Nothing was built - not saving.")
            return spec, built, skipped

        log.info("Saving")
        engine.save()
        log.info("Done. Built %d visualization(s), skipped %d.", len(built), len(skipped))

        return spec, built, skipped
