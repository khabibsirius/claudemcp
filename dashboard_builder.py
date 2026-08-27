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
from insights import NO_MEASURE_TYPES
from ollama_client import OllamaClient, OllamaError
from prompts import DASHBOARD_SYSTEM_PROMPT, build_dashboard_prompt
from qlik_engine import QlikEngine, QlikEngineError, bare_field_name

log = logging.getLogger(__name__)


VALID_EXPRESSION_RE = re.compile(
    r"^(Sum|Count|Avg|Min|Max)\(\s*(DISTINCT\s+)?\[[^\[\]]+\]\s*\)$",
    re.IGNORECASE,
)

_BRACKETED_FIELD_RE = re.compile(r"\[([^\[\]]+)\]")


def expression_field(expression):
    match = _BRACKETED_FIELD_RE.search(expression or "")
    return match.group(1) if match else None


def effective_expression(viz):
    expression = (viz.get("measure_expression") or "").strip()
    if expression:
        return expression

    measure = bare_field_name(viz.get("measure"))
    return f"Sum([{measure}])" if measure else ""


def normalize_visualization(viz):
    clean = dict(viz)
    clean["type"] = resolve_chart_type(clean.get("type")) or (
        clean.get("type") or ""
    ).strip().lower()
    clean["title"] = (clean.get("title") or "").strip()
    clean["dimension"] = bare_field_name(clean.get("dimension"))
    clean["measure"] = bare_field_name(clean.get("measure"))
    clean["measure_expression"] = (clean.get("measure_expression") or "").strip()

    try:
        limit = int(clean.get("limit") or 0)
    except (TypeError, ValueError):
        limit = 0
    clean["limit"] = limit if 0 < limit <= 100 else None

    clean["color"] = resolve_colour(clean.get("color"))

    if clean["type"] == "kpi":
        clean["dimension"] = None

    return clean


MAX_DIMENSION_CARDINALITY = 200


def validate_visualization(viz, field_names=None, field_info=None, engine=None):
    viz_type = viz.get("type")
    if viz_type not in CHART_TYPES:
        return f"unknown chart type {viz_type!r} (expected one of {', '.join(CHART_TYPES)})"

    if not viz.get("title"):
        return "missing title"

    dimension = viz.get("dimension")

    (min_dims, _max_dims), (min_meas, max_meas) = chart_requirements(viz_type)

    if min_dims > 1:
        return (
            f"{viz_type} needs {min_dims} dimensions and a dashboard spec "
            "provides one - use create_chart for it"
        )

    if (min_dims or viz_type in DIMENSIONAL_TYPES) and not dimension:
        return f"{viz_type} needs a dimension to group by"

    if min_meas > 1:
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
            return (
                f"dimension {dimension!r} has the same value in every row, so "
                "grouping by it produces a single bar"
            )

    expression = effective_expression(viz)
    if not expression:
        return "no measure or measure_expression"

    if engine is not None:
        check = engine.check_expression(expression)
        if not check["valid"]:
            detail = check["error"] or (
                "unknown field(s): " + ", ".join(check["bad_fields"])
            )
            return f"measure_expression {expression!r} was rejected by Qlik: {detail}"

        preview, problem = probe_visualization(engine, viz)
        if problem:
            return problem
        if preview:
            viz["preview"] = preview
        return None

    if not VALID_EXPRESSION_RE.match(expression):
        return (
            f"measure_expression {expression!r} isn't a simple aggregation "
            "(Sum/Count/Avg/Min/Max of one field)"
        )

    field = expression_field(expression)
    if field and field_names is not None and field not in field_names:
        return f"measure_expression references unknown field {field!r}"

    return None


PROBE_ROWS = 3


def probe_shape(viz):
    dimensions = viz.get("dimensions")
    if dimensions is None:
        dimension = viz.get("dimension")
        dimensions = [dimension] if dimension else []

    expressions = viz.get("measure_expressions")
    if expressions is None:
        expression = effective_expression(viz)
        expressions = [expression] if expression else []

    return (
        [d for d in dimensions if d and str(d).strip()],
        [e for e in expressions if e and str(e).strip()],
    )


def probe_visualization(engine, viz):
    if engine is None or viz.get("type") in NO_MEASURE_TYPES:
        return None, None

    if viz.get("preview"):
        return viz["preview"], None

    dimensions, expressions = probe_shape(viz)
    if not expressions:
        return None, None

    try:
        result = engine.query(
            dimensions=dimensions, measures=expressions, limit=PROBE_ROWS
        )
    except Exception as e:
        log.debug("Could not probe %r: %s", viz.get("title"), e)
        return None, None

    columns = result.get("columns") or []
    rows = result.get("rows") or []
    if not columns or not rows:
        return None, (
            "it returns no rows from this app's data, so it would render "
            "blank - the fields and the expressions are valid but nothing "
            "comes back when they are combined"
        )

    dimension = dimensions[0] if dimensions else None
    values_by_measure = []
    for offset, expression in enumerate(expressions):
        index = len(dimensions) + offset
        if index >= len(columns):
            break
        column = columns[index]
        values = [
            v for v in (row.get(column) for row in rows)
            if isinstance(v, (int, float)) and not isinstance(v, bool)
        ]
        if not values:
            return None, (
                f"the measure {expression!r} returns no numeric value over "
                f"{'this dimension' if dimension else 'this data'} - it is "
                "probably aggregating a text field, which draws an empty chart"
            )
        values_by_measure.append(values)

    if not values_by_measure:
        return None, (
            "no measure column came back, so there would be nothing to plot"
        )

    values = values_by_measure[0]
    expression = expressions[0]

    preview = {"rows_returned": len(rows), "measure": expression}
    if len(expressions) > 1:
        preview["measures"] = expressions
    total_rows = result.get("total_rows")
    if isinstance(total_rows, int):
        preview["categories"] = total_rows

    if dimension:
        label = rows[0].get(columns[0])
        preview["largest"] = {"label": label, "value": _round_preview(values[0])}
    else:
        preview["value"] = _round_preview(values[0])

    if not any(values):
        preview["warning"] = (
            "every value read back is zero - check this is really the measure "
            "the user wanted, and say so when reporting it"
        )

    return preview, None


def _round_preview(value):
    return round(value, 1) if abs(value) >= 100 else round(value, 4)


_LAYOUT_ORDER = {
    "kpi": 0, "barchart": 1, "linechart": 1, "piechart": 2, "sn-table": 3,
}


def arrange(visualizations):
    def rank(viz):
        raw = (viz.get("type") or "").strip().lower()
        return _LAYOUT_ORDER.get(resolve_chart_type(raw) or raw, 2)

    return sorted(visualizations, key=rank)


def validate_spec(spec):
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


def enrich_fields(engine, fields, max_cardinality=100, sample_size=5, limit=30):
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
    client = client or OllamaClient(model)
    prompt = build_dashboard_prompt(fields, instruction=instruction)
    spec = client.ask_json(prompt, system=DASHBOARD_SYSTEM_PROMPT)

    return validate_spec(spec)


def _signature(viz):
    return (
        viz["type"], viz.get("dimension"), effective_expression(viz),
        viz.get("limit"), viz.get("color"),
    )


def build_dashboard(engine, spec, field_names=None, fields=None, validate_with_engine=True):
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

        signature = _signature(viz)
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

        signature = _signature(viz)
        if signature in seen:
            bad.append((viz, "duplicate of a chart already on this sheet"))
            continue

        seen.add(signature)
        good.append(viz)

    return good, bad


def design_full_dashboard(fields, model=OLLAMA_MODEL, instruction=None, client=None,
                          retries=1, engine=None):
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
            f"the fields that caused them: {rejected}."
        )
        if good:
            top_up += " Do not repeat any of these either: " + "; ".join(
                f"{v['type']} of {v.get('dimension')}" for v in good
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

    spec["visualizations"] = good + [v for v, _ in bad]
    return spec


def build_sheet(engine, title, charts, fields=None, description="Created by AI"):
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
        "built": [
            {
                "type": v["type"],
                "title": v["title"],
                **({"shows": v["preview"]} if v.get("preview") else {}),
            }
            for v in built
        ],
        "skipped": [{"title": v.get("title"), "reason": r} for v, r in skipped],
    }


def run(app_name=APP_NAME, model=OLLAMA_MODEL, instruction=None):
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
