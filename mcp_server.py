import json
import logging
import sys
from typing import Literal

from mcp.server import MCPServer
from pydantic import BaseModel, Field

from chart_specs import CHART_TYPES
from chat_tools import execute
from dashboard_builder import build_sheet, design_dashboard, enrich_fields
import session
from qlik_engine import QlikEngine, QlikEngineError

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(levelname)s %(name)s: %(message)s",
)

log = logging.getLogger(__name__)

SERVER_DEPENDENCIES = [
    "mcp[cli]",
    "websocket-client",
    "ollama",
    "python-dotenv",
]

DATA_FILE_SUFFIXES = (
    ".csv", ".txt", ".tab", ".qvd", ".xlsx", ".xls", ".json", ".xml", ".parquet",
)

ChartType = Literal[tuple(CHART_TYPES)]

assert set(ChartType.__args__) == set(CHART_TYPES), "chart type list drifted"


class Chart(BaseModel):
    type: ChartType = Field(description="Which visualization to draw.")
    title: str = Field(description="Shown as the chart's heading.")
    dimension: str = Field(
        default="",
        description=(
            "Exact field name to group by, unbracketed, e.g. 'Customer Segment'. "
            "Must be left empty for 'kpi'. Prefer a field with few distinct "
            "values - see the qlik://fields resource."
        ),
    )
    measure_expression: str = Field(
        description=(
            "A simple aggregation of one bracketed field: Sum([Sales]), "
            "Count([Order Id]), Count(DISTINCT [Order Id]), Avg([...]), "
            "Min([...]) or Max([...]). SortBy, Limit and Aggr do not work."
        ),
    )


mcp = MCPServer(
    name="qlik-dashboard-builder",
    version="0.4.0",
    dependencies=SERVER_DEPENDENCIES,
    instructions=(
        "Work with a Qlik Sense app. Start with qlik_open - with no argument "
        "it lists the available apps.\n\n"
        "qlik_data_sources shows where the app's data comes from: its "
        "connections, the files in them, and any file's real columns and "
        "sample rows.\n\n"
        "qlik_build_sheet builds a sheet. Either describe it in plain "
        "language and the local model designs it, or pass the charts "
        "yourself, which gives a better result if you know the data. Pick "
        "dimensions with few distinct values - grouping by an id with "
        "thousands of values makes an unreadable chart. Nothing is written to "
        "disk until qlik_save.\n\n"
        "Loading data, editing the load script and cleaning are handled by "
        "this project's chatbot (python chat.py), not by these tools."
    ),
)


def _require_engine() -> QlikEngine:
    return session.engine()


def _connect(app_name: str) -> QlikEngine:
    return session.open_app(app_name)


@mcp.resource("qlik://fields",
    description="""\
The open app's fields, with tags and distinct-value counts, as JSON.""",
)
def qlik_fields_resource() -> str:
    try:
        return json.dumps(_require_engine().get_fields(), indent=2)
    except QlikEngineError as e:
        return json.dumps({"error": str(e)})


@mcp.resource("qlik://sheets",
    description="""\
The open app's sheets, as the Hub's sheet navigator sees them.""",
)
def qlik_sheets_resource() -> str:
    try:
        return json.dumps(_require_engine().list_sheets(), indent=2)
    except QlikEngineError as e:
        return json.dumps({"error": str(e)})


@mcp.tool(
    description="""\
Open a Qlik Sense app, and get your bearings in one call.

Call with no argument to list the apps available. Otherwise pass an app's
title, filename or id. Returns how much data is loaded and how many
sheets exist, so you know whether the app needs data loading (see
qlik_data_sources) or is ready to chart (see the qlik://fields
resource).

This must be the first call - every other tool works on the open app.""",
)
def qlik_open(app: str = "") -> dict:
    if not app:
        if session.connected():
            apps = session.engine().list_apps()
        else:
            with QlikEngine() as scratch:
                apps = scratch.list_apps()
        return {
            "apps": [a["name"] for a in apps],
            "next_step": "Call qlik_open again with one of these names.",
        }

    engine = _connect(app)
    fields = engine.get_fields()
    sheets = engine.list_sheets()

    summary = {
        "app": app,
        "mode": engine.mode,
        "fields": len(fields),
        "sheets": [s["title"] for s in sheets],
    }

    if not fields:
        summary["next_step"] = (
            "This app has no data loaded. Use qlik_data_sources to see what "
            "could be loaded, then load it with this project's assistant or "
            "web editor (python web_app.py) - loading is not exposed over MCP."
        )
    else:
        summary["next_step"] = (
            "Read the qlik://fields resource to see the data, then "
            "qlik_build_sheet to chart it."
        )

    return summary


@mcp.tool(
    description="""\
Find data to load: connections, then folders, then a file's columns.

Call with nothing to list the app's data connections. Pass a connection
name to list what's in it. Pass a path to a data file (.csv, .qvd, .xlsx
and so on) to read that file's tables and real column names WITHOUT
loading it - always do this before writing a LOAD statement, so the
script references columns that exist rather than plausible-looking
guesses.

A load statement refers to a connection by name, as lib://<name>/<file>.
Connection strings are returned with any credentials removed.""",
)
def qlik_data_sources(connection: str = "", path: str = "") -> dict:
    engine = _require_engine()

    if not connection:
        connections = engine.list_connections()
        return {
            "connections": connections,
            "next_step": (
                "Call again with connection=<name> to see what's inside."
                if connections else
                "No data connections exist. Create one in Qlik's Data manager first."
            ),
        }

    if path and path.lower().endswith(DATA_FILE_SUFFIXES):
        preview = engine.preview_file(connection, path)
        preview["lib_path"] = f"lib://{connection}/{path.lstrip('/')}"
        preview["next_step"] = (
            "Load this file with the project's assistant or web editor "
            "(python web_app.py) - loading is not exposed over MCP. Once "
            "loaded, chart it with qlik_build_sheet."
        )
        return preview

    items = engine.browse_connection(connection, path)
    return {
        "connection": connection,
        "path": path,
        "items": items,
        "next_step": "Call again with path=<filename> to see its columns and sample rows.",
    }


@mcp.tool(
    description="""\
Build a sheet of charts and save it. Two ways to call it:

DESCRIBE IT - pass `instruction` in plain language and the local Ollama
model designs the charts by reading the data model itself, e.g.
"sales by region and a trend over time, 4 charts". Nothing else needed.

SPECIFY IT - pass `charts` to say exactly what to build. Do this when you
are choosing the charts yourself, which gives a better result than the
local model: read the qlik://fields resource first, then pass the
charts you want.

Either way every chart is validated against the real data before it is
created: unknown field names, and dimensions with too many distinct
values to read, are skipped with a reason instead of becoming charts that
render blank. Always check the 'skipped' list in the result.""",
)
def qlik_build_sheet(
    instruction: str = "",
    title: str = "",
    charts: list[Chart] | None = None,
) -> dict:
    engine = _require_engine()

    fields = engine.get_fields()
    if not fields:
        return {"error": "No data is loaded in this app. Load data first."}

    if charts:
        specs = [c.model_dump() if hasattr(c, "model_dump") else dict(c) for c in charts]
        sheet_title = title or "New sheet"
        designed_by = "caller"
    else:
        if not (instruction or title):
            raise ValueError(
                "Say what you want: either instruction='sales by region, 4 charts' "
                "to have the local model design it, or charts=[...] to specify it."
            )
        enrich_fields(engine, fields)
        model = session.ensure_model()
        spec = design_dashboard(
            fields, model=model, instruction=instruction or title or None
        )
        specs = spec["visualizations"]
        sheet_title = title or spec["dashboard_title"]
        designed_by = model

    result = build_sheet(engine, sheet_title, specs, fields=fields)
    result["designed_by"] = designed_by

    if result["built"]:
        engine.save()
        result["saved"] = True
    else:
        result["saved"] = False
        result["next_step"] = "Nothing was built - see 'skipped' for why."

    return result


@mcp.tool(
    description="""\
Save the app. Sheets, charts and script changes made in this session
are not on disk until this runs.""",
)
def qlik_save() -> str:
    _require_engine().save()
    return "Saved."


READ_ONLY = {
    "query", "data_model", "read_script", "list_charts",
    "check_expression", "analyze_sheet",
}


def _read(name: str, **arguments) -> dict:
    try:
        return execute(_require_engine(), name, arguments, allowed=READ_ONLY)
    except QlikEngineError as e:
        return {"error": str(e)}


@mcp.tool(
    description="""\
Read actual values out of the open app.

dimensions=["Region"], measures=["Sum([Sales])"] returns each region with
its total, largest first, along with each row's share of the whole - the
shares are computed here rather than left to the caller, because a share
worked out from a truncated list is a share of the page rather than of
the book.

Measures must be a simple aggregation: Sum, Count, Count(DISTINCT ...),
Avg, Min, Max. Anything with Aggr, SortBy or Limit fails to calculate and
comes back empty rather than erroring.
""",
)
def qlik_query(dimensions: list[str] | None = None,
               measures: list[str] | None = None,
               limit: int = 20) -> dict:
    return _read("query", dimensions=dimensions or [],
                 measures=measures or [], limit=limit)


@mcp.tool(
    description="""\
What is loaded: tables, row counts, and every field with its
distinct-value count and null count, plus quality problems such as
constant or mostly-empty columns.

Read this before designing anything. Cardinality is what separates a
category from an identifier, and a bar chart grouped by an order id with
65,000 values renders perfectly and tells you nothing.
""",
)
def qlik_data_model() -> dict:
    return _read("data_model")


@mcp.tool(
    description="""\
Read the app's load script, or one named tab of it.

Reading only. The script is written through the web assistant or
`python chat.py`, where a broken script is syntax-checked and rolled
back, and where the change is recorded against a person.
""",
)
def qlik_script(tab: str = "") -> dict:
    return _read("read_script", tab=tab)


@mcp.tool(
    description="""\
Every chart in the app with its id, type, title, dimensions and
measure expressions, and every sheet with how many charts are on it.

Empty sheets are included: a count that silently skipped them made
"you have 3 sheets" wrong.
""",
)
def qlik_list_charts() -> dict:
    return _read("list_charts")


@mcp.tool(
    description="""\
Ask Qlik whether an expression is valid, without building anything.

Returns the engine's own error and any field names that do not exist.
Worth calling before qlik_build_sheet: Qlik does not reject a chart that
references a field which isn't there - it creates the object and renders
an empty box.
""",
)
def qlik_check_expression(expression: str) -> dict:
    return _read("check_expression", expression=expression)


@mcp.tool(
    description="""\
Read the real numbers behind a sheet's charts and return what they show.

Totals, the largest and smallest category, concentration and
period-on-period change - all computed in Python from the rows, not left
to a model to derive. Shares and totals are withheld for a measure that
does not add up, such as an average or a margin.
""",
)
def qlik_analyze_sheet(sheet: str = "", chart_ids: list[str] | None = None) -> dict:
    return _read("analyze_sheet", sheet=sheet, chart_ids=chart_ids or [])


if __name__ == "__main__":
    mcp.run()
