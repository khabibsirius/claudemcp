import re

from config import APP_NAME, OLLAMA_MODEL
from qlik_engine import QlikEngine, QlikEngineError
from ollama_client import OllamaClient
from prompts import DASHBOARD_SYSTEM_PROMPT, build_dashboard_prompt


# Only allow simple single-field aggregations. Anything else (SortBy, Limit,
# Aggr, Rank, nested expressions, etc.) is a known source of blank/failed
# charts - the object gets created fine but silently fails to calculate.
VALID_EXPRESSION_RE = re.compile(
    r"^(Sum|Count|Avg|Min|Max)\(\s*(DISTINCT\s+)?\[[^\[\]]+\]\s*\)$",
    re.IGNORECASE
)


def _expression_field(expression):
    """Pulls the bracketed field name out of a simple aggregation expression."""
    match = re.search(r"\[([^\[\]]+)\]", expression or "")
    return match.group(1) if match else None


def _clean_field_name(name):
    """The model is told measure_expression needs brackets around field names
    (e.g. "Sum([Sales])") but that dimension should be a plain field name.
    It doesn't always keep that straight and sometimes sends dimension as
    "[Customer Segment]" too - which then fails the real-field check even
    though the field exists, and would go to Qlik as a literal field name
    containing brackets if left uncorrected. Strip them so it matches."""
    if not name:
        return name
    name = name.strip()
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1].strip()
    return name


def validate_visualization(viz, field_names):
    """Checks a single visualization spec against the app's real field list
    and expression rules. Returns an error string, or None if it's valid."""

    dimension = _clean_field_name(viz.get("dimension"))
    expression = viz.get("measure_expression") or ""

    if dimension and dimension not in field_names:
        return f"dimension '{dimension}' is not a real field in this app"

    if not expression:
        return "missing measure_expression"

    if not VALID_EXPRESSION_RE.match(expression.strip()):
        return f"measure_expression '{expression}' isn't a simple aggregation (Sum/Count/Avg/Min/Max of one field)"

    field = _expression_field(expression)
    if field and field not in field_names:
        return f"measure_expression references unknown field '{field}'"

    return None


CHART_BUILDERS = {
    "kpi": lambda engine, viz: engine.create_kpi(
        measure=viz.get("measure"),
        measure_expression=viz.get("measure_expression"),
        title=viz.get("title"),
    ),
    "barchart": lambda engine, viz: engine.create_chart(
        "barchart", viz.get("title"),
        dimension=viz.get("dimension"),
        measure=viz.get("measure"),
        measure_expression=viz.get("measure_expression"),
    ),
    "linechart": lambda engine, viz: engine.create_chart(
        "linechart", viz.get("title"),
        dimension=viz.get("dimension"),
        measure=viz.get("measure"),
        measure_expression=viz.get("measure_expression"),
    ),
    "piechart": lambda engine, viz: engine.create_chart(
        "piechart", viz.get("title"),
        dimension=viz.get("dimension"),
        measure=viz.get("measure"),
        measure_expression=viz.get("measure_expression"),
    ),
    "table": lambda engine, viz: engine.create_chart(
        "table", viz.get("title"),
        dimension=viz.get("dimension"),
        measure=viz.get("measure"),
        measure_expression=viz.get("measure_expression"),
        colspan=24, rowspan=6,
    ),
}


def design_dashboard(fields, model=OLLAMA_MODEL, instruction=None):
    """Asks the LLM to turn a field list into a dashboard spec (dict).

    instruction: optional free-text request steering what gets designed.
    """

    client = OllamaClient(model)

    prompt = build_dashboard_prompt(fields, instruction=instruction)

    spec = client.ask_json(prompt, system=DASHBOARD_SYSTEM_PROMPT)

    if "dashboard_title" not in spec or "visualizations" not in spec:
        raise ValueError(f"Malformed dashboard spec from model: {spec}")

    return spec


def build_dashboard(engine: QlikEngine, spec: dict, field_names=None):
    """Creates the sheet and charts in Qlik from a dashboard spec.

    field_names: set of real field names in the app, used to reject
    visualizations that reference fields the model hallucinated. If not
    given, that check is skipped (not recommended).
    """

    print(f"Creating sheet: {spec['dashboard_title']}")
    engine.create_sheet(spec["dashboard_title"])

    built = []
    skipped = []

    for viz in spec["visualizations"]:

        viz_type = viz.get("type")
        builder = CHART_BUILDERS.get(viz_type)

        if "dimension" in viz:
            viz["dimension"] = _clean_field_name(viz.get("dimension"))

        if builder is None:
            skipped.append((viz, f"unknown chart type '{viz_type}'"))
            print(f"  ! skipped {viz.get('title')!r} (unknown chart type '{viz_type}')")
            continue

        if viz_type != "kpi" or viz.get("measure_expression"):
            # KPIs still get their expression checked; only "table" with no
            # measure at all (rare) would skip validation entirely.
            error = validate_visualization(viz, field_names) if field_names else None
            if error:
                skipped.append((viz, error))
                print(f"  ! skipped {viz.get('title')!r} ({error})")
                continue

        try:
            print(f"  + {viz_type}: {viz.get('title')}")
            builder(engine, viz)
            built.append(viz)
        except QlikEngineError as e:
            skipped.append((viz, str(e)))
            print(f"  ! skipped ({e})")

    return built, skipped


def run(app_name=APP_NAME, model=OLLAMA_MODEL, instruction=None):
    """End-to-end: connect to Qlik, inspect the data model, ask the AI to
    design a dashboard, build it, and save.

    instruction: optional free-text request steering what gets designed,
    e.g. "focus on sales by region and show a trend over time".
    """

    engine = QlikEngine()

    try:
        print(f"Opening app '{app_name}'...")
        engine.open_app(app_name)

        print("Reading data model...")
        fields = engine.get_fields()

        if not fields:
            raise RuntimeError(
                "No fields found in this app. Is data loaded into it?"
            )

        print(f"Found {len(fields)} fields. Asking {model} to design a dashboard...")
        spec = design_dashboard(fields, model=model, instruction=instruction)

        field_names = {f["name"] for f in fields}
        built, skipped = build_dashboard(engine, spec, field_names=field_names)

        print("Saving...")
        engine.save()

        print(f"Done. Built {len(built)} visualization(s), skipped {len(skipped)}.")
        if skipped:
            print("Skipped:")
            for viz, reason in skipped:
                print(f"  - {viz.get('title')!r}: {reason}")

        return spec, built, skipped

    finally:
        engine.close()


if __name__ == "__main__":
    run()