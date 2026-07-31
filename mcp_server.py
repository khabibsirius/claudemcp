"""
MCP server for building dashboards in Qlik Sense (Desktop or Enterprise/Server)
directly from Claude Code, Claude Desktop, or any other MCP client.

Connection mode is controlled via .env (QLIK_MODE=desktop|enterprise) - see
README.md for setup. Run with:

    python mcp_server.py

and point your MCP client at this script (stdio transport).
"""

from typing import Optional

from mcp.server import MCPServer

from config import APP_NAME, OLLAMA_MODEL
from qlik_engine import QlikEngine, QlikEngineError
from dashboard_builder import design_dashboard, build_dashboard

mcp = MCPServer(
    name="qlik-dashboard-builder",
    version="0.1.0",
    instructions=(
        "Tools for inspecting a Qlik Sense app's data model and creating "
        "sheets/charts in it via the Qlik Engine API. Call qlik_connect "
        "first. For a one-shot AI-designed dashboard, use "
        "qlik_build_ai_dashboard instead of the individual tools."
    ),
)

# Single shared connection for this server process. Fine for the intended
# single-user, single-app workflow; qlik_connect() replaces it if called
# again (e.g. to switch apps).
_state = {"engine": None}


def _require_engine() -> QlikEngine:
    if _state["engine"] is None:
        raise QlikEngineError("Not connected. Call qlik_connect first.")
    return _state["engine"]


# ----------------------------------------------------------------------
# Low-level tools - building blocks Claude can compose step by step
# ----------------------------------------------------------------------

@mcp.tool()
def qlik_connect(app_name: str = APP_NAME) -> str:
    """Connect to a Qlik Sense app and open it. Call this before any other
    qlik_* tool. Connection mode (Desktop vs Enterprise) and credentials are
    read from this server's .env configuration, not from arguments."""
    if _state["engine"] is not None:
        _state["engine"].close()

    engine = QlikEngine()
    engine.open_app(app_name)
    _state["engine"] = engine

    fields = engine.get_fields()
    return f"Connected to app '{app_name}' ({engine.mode} mode). {len(fields)} fields available."


@mcp.tool()
def qlik_list_fields() -> list[dict]:
    """List every field in the connected app's data model: name, tags
    (e.g. '$numeric', '$key'), and which source table(s) it belongs to.
    Always check this before choosing dimensions/measures - field names
    must match exactly, character for character."""
    engine = _require_engine()
    return engine.get_fields()


@mcp.tool()
def qlik_create_sheet(title: str, description: str = "Created by AI") -> str:
    """Create a new sheet in the connected app and make it the active sheet
    for subsequent qlik_create_chart calls."""
    engine = _require_engine()
    engine.create_sheet(title, description=description)
    return f"Created sheet '{title}' (qId={engine.sheet_id})."


@mcp.tool()
def qlik_create_chart(
    chart_type: str,
    title: str,
    dimension: Optional[str] = None,
    measure: Optional[str] = None,
    measure_expression: Optional[str] = None,
) -> str:
    """Add a chart to the currently active sheet (call qlik_create_sheet
    first). chart_type must be one of: 'kpi', 'barchart', 'linechart',
    'piechart', 'table'. dimension is the exact field name to group by
    (omit for kpi). measure_expression must be a single simple aggregation
    like 'Sum([Sales])', 'Count([Order Id])', 'Avg([Field Name])' -
    anything more complex (SortBy, Limit, Aggr, nested expressions) will
    fail to calculate and render blank. Use qlik_list_fields first;
    invented field names create a chart object but it silently shows no
    data."""
    engine = _require_engine()
    engine.create_chart(
        chart_type, title,
        dimension=dimension, measure=measure, measure_expression=measure_expression,
    )
    return f"Added {chart_type} '{title}' to the current sheet."


@mcp.tool()
def qlik_save() -> str:
    """Save the connected app, persisting any sheets/charts created in this
    session to disk."""
    engine = _require_engine()
    engine.save()
    return "Saved."


@mcp.tool()
def qlik_list_sheets() -> list[dict]:
    """List every sheet in the connected app the way the Qlik Sense Hub's
    sheet navigator sees it (title and chart count), to confirm a sheet is
    actually discoverable rather than just present as a raw object."""
    engine = _require_engine()

    response = engine.send(
        "CreateSessionObject",
        handle=engine.app_handle,
        params=[{
            "qInfo": {"qType": "SheetList"},
            "qAppObjectListDef": {
                "qType": "sheet",
                "qData": {
                    "title": "/qMetaDef/title",
                    "description": "/qMetaDef/description",
                    "cells": "/cells",
                    "rank": "/rank",
                }
            }
        }]
    )
    list_handle = response["result"]["qReturn"]["qHandle"]
    layout = engine.send("GetLayout", handle=list_handle)
    items = layout["result"]["qLayout"]["qAppObjectList"]["qItems"]

    return [
        {
            "qId": item["qInfo"]["qId"],
            "title": item.get("qData", {}).get("title", ""),
            "chart_count": len(item.get("qData", {}).get("cells", []) or []),
        }
        for item in items
    ]


@mcp.tool()
def qlik_disconnect() -> str:
    """Close the connection to the Qlik Sense app."""
    if _state["engine"] is not None:
        _state["engine"].close()
        _state["engine"] = None
        return "Disconnected."
    return "Was not connected."


# ----------------------------------------------------------------------
# High-level tool - the whole AI-design-and-build flow in one call
# ----------------------------------------------------------------------

@mcp.tool()
def qlik_build_ai_dashboard(app_name: str = APP_NAME, ollama_model: str = OLLAMA_MODEL) -> dict:
    """One-shot: connect to an app, read its data model, ask a local Ollama
    LLM to design a dashboard (title + a handful of KPIs/charts), build it
    as a new sheet, skip anything that references a non-existent field or
    an unsafe expression, and save. Returns what was built and what was
    skipped, with reasons. Use this instead of the individual qlik_* tools
    when you just want "build me a dashboard" in one step; use the
    individual tools when you want to design the dashboard yourself."""
    engine = QlikEngine()
    try:
        engine.open_app(app_name)
        fields = engine.get_fields()
        if not fields:
            return {"error": f"No fields found in app '{app_name}'. Is data loaded into it?"}

        spec = design_dashboard(fields, model=ollama_model)
        field_names = {f["name"] for f in fields}
        built, skipped = build_dashboard(engine, spec, field_names=field_names)
        engine.save()

        return {
            "dashboard_title": spec["dashboard_title"],
            "built": [v.get("title") for v in built],
            "skipped": [{"title": v.get("title"), "reason": reason} for v, reason in skipped],
        }
    finally:
        engine.close()


if __name__ == "__main__":
    mcp.run()
