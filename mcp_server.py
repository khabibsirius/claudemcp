"""MCP server for working with a Qlik Sense app.

Four tools, deliberately: open an app, look at where data comes from, build a
sheet, save.

    Home          qlik_open
    Data manager  qlik_data_sources
    Sheet         qlik_build_sheet, qlik_save

Everything else - writing and running load scripts, cleaning data, exploring
values - lives in the chatbot instead, where it is reached by asking rather
than by filling in tool arguments by hand:

    python chat.py

Connection mode and credentials come from .env - see .env.example. Run with:

    python mcp_server.py

and point your MCP client at this script (stdio transport).
"""

import json
import logging
import sys
from typing import Literal

from mcp.server import MCPServer
from pydantic import BaseModel, Field

from chart_specs import CHART_TYPES
from dashboard_builder import build_sheet, design_dashboard, enrich_fields
import session
from qlik_engine import QlikEngine, QlikEngineError

# stdio is the MCP transport: anything written to stdout is parsed as
# protocol. Logs must go to stderr or they corrupt the session.
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(levelname)s %(name)s: %(message)s",
)

log = logging.getLogger(__name__)

# `mcp dev` does not run this file in your current Python environment. It
# launches it through `uv run` in a throwaway one containing only `mcp` plus
# whatever is declared here - so without this list the server dies on
# `import websocket` before registering a single tool, and the Inspector
# connects to nothing. `mcp[cli]` is included because the CLI's own bootstrap
# passes plain `mcp`, leaving its `mcp run` entrypoint without typer.
# Keep in sync with requirements.txt.
SERVER_DEPENDENCIES = [
    "mcp[cli]",
    "websocket-client",
    "ollama",
    "python-dotenv",
]

# Files qlik_data_sources previews rather than treating as a folder.
DATA_FILE_SUFFIXES = (
    ".csv", ".txt", ".tab", ".qvd", ".xlsx", ".xls", ".json", ".xml", ".parquet",
)

# Spelling the choices out as Literals puts them in the tool's JSON schema as
# an enum, so a caller can see the valid values without going and looking
# them up in Qlik.
# Built from CHART_TYPES so the tool schema always offers exactly what the
# engine can create - every Qlik chart type, not the five we started with.
ChartType = Literal[tuple(CHART_TYPES)]  # type: ignore[valid-type]

assert set(ChartType.__args__) == set(CHART_TYPES), "chart type list drifted"


class Chart(BaseModel):
    """One chart on a sheet."""

    type: ChartType = Field(description="Which visualization to draw.")
    title: str = Field(description="Shown as the chart's heading.")
    dimension: str = Field(
        default="",
        description=(
            "Exact field name to group by, unbracketed, e.g. 'Customer Segment'. "
            "Must be left empty for 'kpi'. Prefer a field with few distinct "
            "values - see qlik_data_model."
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
    """The shared engine - the same one the web UI and chat assistant use."""
    return session.engine()


def _connect(app_name: str) -> QlikEngine:
    return session.open_app(app_name)


# ----------------------------------------------------------------------
# Resources - read-only context the client can browse
# ----------------------------------------------------------------------

@mcp.resource("qlik://fields")
def qlik_fields_resource() -> str:
    """The open app's fields, with tags and distinct-value counts, as JSON."""
    try:
        return json.dumps(_require_engine().get_fields(), indent=2)
    except QlikEngineError as e:
        return json.dumps({"error": str(e)})


@mcp.resource("qlik://sheets")
def qlik_sheets_resource() -> str:
    """The open app's sheets, as the Hub's sheet navigator sees them."""
    try:
        return json.dumps(_require_engine().list_sheets(), indent=2)
    except QlikEngineError as e:
        return json.dumps({"error": str(e)})


# ----------------------------------------------------------------------
# Home
# ----------------------------------------------------------------------

@mcp.tool()
def qlik_open(app: str = "") -> dict:
    """Open a Qlik Sense app, and get your bearings in one call.

    Call with no argument to list the apps available. Otherwise pass an app's
    title, filename or id. Returns how much data is loaded and how many
    sheets exist, so you know whether the app needs data loading (see
    qlik_data_sources) or is ready to chart (see qlik_data_model).

    This must be the first call - every other tool works on the open app."""

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


# ----------------------------------------------------------------------
# Prepare - Data manager (where the data comes from)
# ----------------------------------------------------------------------

@mcp.tool()
def qlik_data_sources(connection: str = "", path: str = "") -> dict:
    """Find data to load: connections, then folders, then a file's columns.

    Call with nothing to list the app's data connections. Pass a connection
    name to list what's in it. Pass a path to a data file (.csv, .qvd, .xlsx
    and so on) to read that file's tables and real column names WITHOUT
    loading it - always do this before writing a LOAD statement, so the
    script references columns that exist rather than plausible-looking
    guesses.

    A load statement refers to a connection by name, as lib://<name>/<file>.
    Connection strings are returned with any credentials removed."""

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






@mcp.tool()
def qlik_build_sheet(
    instruction: str = "",
    title: str = "",
    charts: list[Chart] | None = None,
) -> dict:
    """Build a sheet of charts and save it. Two ways to call it:

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
    render blank. Always check the 'skipped' list in the result."""

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
            raise QlikEngineError(
                "Say what you want: either instruction='sales by region, 4 charts' "
                "to have the local model design it, or charts=[...] to specify it."
            )
        # Real sample values and distinct counts, so the local model picks
        # dimensions that exist and can actually be read on a chart.
        enrich_fields(engine, fields)
        # The session's model, not a separate configured one: designing with
        # a different model loads a second copy into memory beside the one
        # already running the assistant. Resolved on demand so Ollama can be
        # started after this server.
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



@mcp.tool()
def qlik_save() -> str:
    """Save the app. Sheets, charts and script changes made in this session
    are not on disk until this runs."""
    _require_engine().save()
    return "Saved."


if __name__ == "__main__":
    mcp.run()
