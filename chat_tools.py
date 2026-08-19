"""The actions the chatbot can take, and their schemas for the local model.

This is the whole capability surface: data connections, the load script,
reloading, reading data, and building sheets. The MCP server deliberately
exposes only a small part of this - the rest is meant to be reached by
talking to the chatbot rather than by calling tools by hand.

Each entry is (schema for the model, python function). Functions take the
engine plus keyword arguments and return something JSON-serialisable.
"""

import json
import logging

from chart_specs import describe_chart_types
from config import CHAT_HISTORY_CHARS, CHAT_MAX_STEPS, OLLAMA_NUM_CTX
from qlik_engine import QlikNotFoundError
from dashboard_builder import build_sheet as _build_sheet
from dashboard_builder import design_full_dashboard, enrich_fields
from data_prep import (
    GENERATED_TAB,
    append_to_tab,
    delete_tab,
    describe_data_model,
    generate_load_script,
    get_tab,
    set_tab,
    tab_names,
)
import glossary
from insights import add_shares, analyse_sheet, labels_in, snap_labels

log = logging.getLogger(__name__)

# Reloading rebuilds the data model, and replacing the whole script throws
# away hand-written work. The chat loop asks before running these.
DESTRUCTIVE = {"reload_data"}

# What the web editor lets the model touch. Reloading is deliberately absent:
# there it is a button the person presses, exactly as in Qlik's own editor.
LOAD_EDITOR_TOOLS = {
    "data_sources", "add_data_source", "read_script",
    "build_load_script", "write_script", "data_model", "query",
}

DATA_FILE_SUFFIXES = (
    ".csv", ".txt", ".tab", ".qvd", ".xlsx", ".xls", ".json", ".xml", ".parquet",
)


# ----------------------------------------------------------------------
# Actions
# ----------------------------------------------------------------------

def open_app(engine, app=""):
    """Open an app, or list them when no name is given."""
    if not app:
        return {"apps": [a["name"] for a in engine.list_apps()]}

    engine.open_app(app)
    fields = engine.get_fields()
    return {
        "app": app,
        "fields": len(fields),
        "sheets": [s["title"] for s in engine.list_sheets()],
        "has_data": bool(fields),
    }


def data_sources(engine, connection="", path=""):
    """Connections, then folder contents, then a file's columns and rows."""
    if not connection:
        return {"connections": engine.list_connections()}

    if path and path.lower().endswith(DATA_FILE_SUFFIXES):
        preview = engine.preview_file(connection, path, sample_rows=5)
        preview["lib_path"] = f"lib://{connection}/{path.lstrip('/')}"
        return preview

    return {
        "connection": connection,
        "path": path,
        "items": engine.browse_connection(connection, path),
    }


def add_data_source(engine, name, folder_path):
    """Register a folder so its files can be loaded."""
    return engine.create_connection(name, folder_path)


def read_script(engine, tab=""):
    """The load script, or one tab of it."""
    script = engine.get_script()
    names = tab_names(script)

    if tab:
        body = get_tab(script, tab)
        if body is None:
            return {"error": f"No tab {tab!r}. Tabs: {', '.join(names)}"}
        return {"tabs": names, "tab": tab, "content": body}

    return {"tabs": names, "content": script}


def write_script(engine, content="", tab=GENERATED_TAB, mode="replace_tab"):
    """Write the load script. Syntax-checked; rolled back if it won't parse."""
    current = engine.get_script()

    if mode == "replace_tab":
        updated = set_tab(current, content, tab_name=tab)
    elif mode == "append":
        updated = append_to_tab(current, content, tab_name=tab)
    elif mode == "replace_all":
        updated = content
    elif mode == "delete_tab":
        updated = delete_tab(current, tab)
    else:
        return {"error": f"mode must be replace_tab, append, replace_all or delete_tab"}

    engine.set_script(updated)
    return {"ok": True, "tabs": tab_names(updated), "note": "Not reloaded yet."}


def build_load_script(engine, sources, mode="separate", drop_fields=None,
                      trim_text=True, null_tokens=None, table_name="",
                      derived=None):
    """Generate a clean LOAD script for one or more files."""
    previewed = []
    for source in sources or []:
        # Sample rows are what stop a numeric column being wrapped in Trim()
        # and silently turned into text.
        preview = engine.preview_file(source["connection"], source["path"], sample_rows=20)
        for table in preview["tables"]:
            previewed.append({
                "connection": source["connection"],
                "path": source["path"],
                "table": source.get("table") or table["name"],
                # The name INSIDE the file (an Excel sheet), which the FROM
                # clause's format spec needs - distinct from "table", the
                # name the load gives the result.
                "file_table": table["name"],
                "columns": table["columns"],
                "sample_rows": table.get("sample_rows", []),
            })

    if not previewed:
        return {"error": "No readable tables in those sources."}

    return generate_load_script(
        previewed, mode=mode, drop_fields=drop_fields or (),
        trim_text=trim_text, null_tokens=null_tokens or (),
        table_name=table_name or None, derived=derived or (),
    )


def reload_data(engine):
    """Run the load script, rebuilding the app's data."""
    result = engine.reload_data()
    if result.get("success"):
        result["tables"] = [
            {"name": t["name"], "rows": t["rows"]} for t in engine.get_tables()
        ]
    return result


def data_model(engine):
    """Tables, fields, distinct counts, nulls, and quality problems."""
    return describe_data_model(engine)


def query(engine, dimensions=None, measures=None, limit=20):
    """Read actual values out of the data, with each row's share worked out.

    The shares are added here rather than left to the model. Asked what
    proportion something is, a model with a column of numbers in front of it
    will do the division in its reply, and get the last digit wrong often
    enough to matter to someone reading a deposit book.
    """
    result = engine.query(
        dimensions=dimensions or [], measures=measures or [], limit=limit
    )
    return add_shares(result=result, engine=engine,
                      dimensions=dimensions, measures=measures)


def build_dashboard(engine, instruction, title="", model=None):
    """Design a sheet from a plain-language brief and build it.

    Deliberately not "here is a list of charts, build them": a small local
    model asked to emit chart specs mid-conversation produces worse
    dashboards than the dedicated design prompt does, which sees the whole
    field list with distinct counts and real sample values. So the model
    passes the brief through and the tuned pipeline does the designing.
    """
    fields = engine.get_fields()
    if not fields:
        return {"error": "No data is loaded in this app. Load data first."}

    if model is None:
        # The one model the whole session is using. Designing with a
        # different one would pull a second model into memory alongside the
        # one already loaded - which on a machine also running Qlik is the
        # difference between working and swapping.
        import session

        model = session.ensure_model()

    enrich_fields(engine, fields)
    spec = design_full_dashboard(
        fields, model=model, instruction=instruction or title or None, engine=engine
    )

    result = _build_sheet(
        engine, title or spec["dashboard_title"], spec["visualizations"], fields=fields
    )
    if result["built"]:
        engine.save()
        result["saved"] = True
    return result


def create_chart(engine, chart_type, title, dimensions=None, measures=None,
                 sheet_title=None, color=None, limit=None):
    """Create one chart, with as many dimensions and measures as it needs.

    build_dashboard designs a whole sheet but carries one dimension and one
    measure per chart, so the types that need more - sankey, scatter, mekko,
    a combo chart with two measures - cannot be expressed through it at all.
    This is the way to build those.
    """
    # A sheet name that already exists means "put it there", not "make
    # another one with the same name" - which is what it used to do, leaving
    # duplicate sheets and the chart nowhere the person was looking.
    if sheet_title:
        try:
            engine.open_sheet(sheet_title)
        except QlikNotFoundError:
            # Only when it genuinely isn't there. An ambiguous name must
            # propagate: creating another sheet with the same title makes
            # the ambiguity worse every time.
            engine.create_sheet(sheet_title)
    elif engine.sheet_handle is None:
        engine.create_sheet(title)

    engine.create_chart(
        chart_type, title,
        dimensions=list(dimensions or []),
        measure_expressions=list(measures or []),
        colour=color, limit=limit,
    )
    engine.save()

    return {
        "created": {"type": chart_type, "title": title},
        "sheet": sheet_title or engine.sheet_id,
        "saved": True,
    }


def list_charts(engine):
    """Every chart in the app, with the id needed to change one."""
    return {"charts": engine.list_charts()}


def analyze_sheet(engine, sheet="", chart_ids=None):
    """Read the numbers behind a sheet's charts and reduce them to facts."""
    return analyse_sheet(engine, sheet=sheet or None, chart_ids=chart_ids or None)


def edit_chart(engine, chart_id, title=None, measure_expression=None,
               measure_label=None, dimension=None, color=None, limit=None):
    """Change an existing chart in place."""
    result = engine.update_chart(
        chart_id, title=title, measure_expression=measure_expression,
        measure_label=measure_label, dimension=dimension,
        colour=color, limit=limit,
    )
    if result.get("changed"):
        engine.save()
        result["saved"] = True
    return result


def check_expression(engine, expression):
    """Ask Qlik whether an expression is valid, without building anything."""
    return engine.check_expression(expression)


def save(engine):
    """Persist the app to disk."""
    engine.save()
    return {"saved": True}


# ----------------------------------------------------------------------
# Schemas handed to the model
# ----------------------------------------------------------------------

def _tool(name, description, properties=None, required=None):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties or {},
                "required": required or [],
            },
        },
    }


_STRING = {"type": "string"}
_STRINGS = {"type": "array", "items": {"type": "string"}}

TOOLS = [
    _tool(
        "open_app",
        "Open a Qlik app by name. Call with no arguments to list available apps.",
        {"app": _STRING},
    ),
    _tool(
        "data_sources",
        "Find data. No arguments lists data connections. A connection name "
        "lists its files. A connection plus a data file path returns that "
        "file's real column names and a few sample rows, without loading it. "
        "Always do this before writing a LOAD statement.",
        {"connection": _STRING, "path": _STRING},
    ),
    _tool(
        "add_data_source",
        "Register a folder on disk as a data connection so its files can be "
        "loaded, e.g. the user's Downloads folder. Needed before loading from "
        "any folder not already listed by data_sources.",
        {"name": _STRING, "folder_path": _STRING},
        ["name", "folder_path"],
    ),
    _tool(
        "read_script",
        "Read the app's load script, or one named tab of it. Always read "
        "before writing so existing work is not overwritten.",
        {"tab": _STRING},
    ),
    _tool(
        "build_load_script",
        "Generate a clean LOAD script for one or more files. Each source is "
        "{connection, path}. mode 'concatenate' merges the files into one "
        "table; 'separate' keeps one table per file. drop_fields removes "
        "columns; null_tokens turns placeholders like 'N/A' into real nulls. "
        "\n\n"
        "USE `derived` TO ADD NEW COLUMNS. Each entry is {name, expression} "
        "where expression is Qlik script, e.g. "
        "{\"name\": \"Year\", \"expression\": \"Year([Report Date])\"} or "
        "{\"name\": \"HighDeposit\", \"expression\": \"If([SUM] > 100, 1, 0)\"}. "
        "This is how a calculated field is created - you never need to write "
        "a LOAD statement by hand to add one. The result is syntax-checked "
        "before it is applied, so a wrong expression is reported rather than "
        "breaking the app.\n\n"
        "Returns the script - it does not apply it.",
        {
            "sources": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"connection": _STRING, "path": _STRING, "table": _STRING},
                    "required": ["connection", "path"],
                },
            },
            "mode": {"type": "string", "enum": ["separate", "concatenate"]},
            "drop_fields": _STRINGS,
            "null_tokens": _STRINGS,
            "table_name": _STRING,
            "derived": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"name": _STRING, "expression": _STRING},
                    "required": ["name", "expression"],
                },
            },
        },
        ["sources"],
    ),
    _tool(
        "write_script",
        "Write the load script. mode 'replace_tab' (default) rewrites one tab "
        "and leaves the others alone; 'append' adds to a tab; 'replace_all' "
        "overwrites everything; 'delete_tab' removes a tab. The script is "
        "syntax-checked and rolled back if it does not parse. Does not reload.",
        {
            "content": _STRING,
            "tab": _STRING,
            "mode": {
                "type": "string",
                "enum": ["replace_tab", "append", "replace_all", "delete_tab"],
            },
        },
    ),
    _tool(
        "reload_data",
        "Run the load script and rebuild the app's data from its sources. "
        "Destructive: the current data is replaced. Returns the resulting "
        "tables and row counts, or the engine's errors if it failed.",
    ),
    _tool(
        "data_model",
        "What data is loaded: tables, row counts, and every field with its "
        "distinct-value count and null count, plus quality problems such as "
        "constant or mostly-empty columns. Read this before building charts.",
    ),
    _tool(
        "query",
        "Read actual values. dimensions=['Region'], measures=['Sum([Sales])'] "
        "returns each region with its total, sorted descending, so a small "
        "limit gives a real top-N. Measures alone return a single number.",
        {"dimensions": _STRINGS, "measures": _STRINGS, "limit": {"type": "integer"}},
    ),
    _tool(
        "build_dashboard",
        "Build a sheet of charts and save it. Any chart type in AVAILABLE CHART "
        "TYPES can be used - say which you want in the instruction, e.g. "
        "\"a gauge of total sales and a treemap by region\". Pass the user's "
        "request through "
        "as `instruction`, in their own words - including any number of "
        "charts they asked for and any fields they named. The charts are "
        "designed from the app's real data, validated, and anything that "
        "would render blank is skipped. Do not try to specify the charts "
        "yourself; describe what is wanted and let this design it.",
        {"instruction": _STRING, "title": _STRING},
        ["instruction"],
    ),
    _tool(
        "create_chart",
        "Create ONE chart with as many dimensions and measures as it needs, "
        "and save it. This is the only way to build the types that take more "
        "than one of either - sankey (2-5 dimensions), scatter (2-3 "
        "measures), mekko and grid (2 dimensions), a combo chart with two "
        "measures. build_dashboard cannot express those: it carries one "
        "dimension and one measure per chart. dimensions are exact field "
        "names, in flow order for a sankey. measures are Qlik expressions "
        "such as Sum([SUM]). Give sheet_title to start a new sheet, or omit "
        "it to add to the current one. Check AVAILABLE CHART TYPES for what "
        "each type needs.",
        {
            "chart_type": _STRING,
            "title": _STRING,
            "dimensions": _STRINGS,
            "measures": _STRINGS,
            "sheet_title": _STRING,
            "color": _STRING,
            "limit": {"type": "integer"},
        },
        ["chart_type", "title"],
    ),
    _tool(
        "list_charts",
        "Every chart in the app with its id, type, title, dimensions and "
        "measure expressions. The id is the only way to identify a chart for "
        "editing - get it from here before calling edit_chart.",
    ),
    _tool(
        "edit_chart",
        "Change a chart that already exists, in place. Only the arguments you "
        "pass are touched; everything else about the chart is preserved. Use "
        "it to fix a wrong measure, rename a chart, regroup it by a different "
        "field, recolour it, or limit it to a top N. measure_expression may "
        "be any valid Qlik expression, including set analysis such as "
        "Sum({<[Region]={'Almaty'}>} [SUM]) or Aggr(...) - it is checked by "
        "Qlik before being applied and rejected with the reason if wrong.",
        {
            "chart_id": _STRING,
            "title": _STRING,
            "measure_expression": _STRING,
            "measure_label": _STRING,
            "dimension": _STRING,
            "color": _STRING,
            "limit": {"type": "integer"},
        },
        ["chart_id"],
    ),
    _tool(
        "check_expression",
        "Ask Qlik whether an expression is valid, without building anything. "
        "Returns the engine's own error message and any field names that do "
        "not exist. Use it to try a complicated expression before committing "
        "it to a chart.",
        {"expression": _STRING},
        ["expression"],
    ),
    _tool(
        "analyze_sheet",
        "Read the actual numbers behind the charts on a sheet and return the "
        "facts they show: totals, the largest and smallest category, its "
        "share of the total, how concentrated the top few are, and the change "
        "across a time dimension. Call this before saying anything about what "
        "the data means - building a chart does not show you its values, and "
        "the arithmetic here is done against the live app rather than "
        "estimated. Omit `sheet` for the one just built.",
        {"sheet": _STRING, "chart_ids": _STRINGS},
    ),
    _tool("save", "Save the app to disk. Nothing persists until this runs."),
]

FUNCTIONS = {
    "open_app": open_app,
    "data_sources": data_sources,
    "add_data_source": add_data_source,
    "read_script": read_script,
    "build_load_script": build_load_script,
    "write_script": write_script,
    "reload_data": reload_data,
    "data_model": data_model,
    "query": query,
    "build_dashboard": build_dashboard,
    "create_chart": create_chart,
    "list_charts": list_charts,
    "edit_chart": edit_chart,
    "check_expression": check_expression,
    "analyze_sheet": analyze_sheet,
    "save": save,
}

assert {t["function"]["name"] for t in TOOLS} == set(FUNCTIONS), "tool list drifted"


def _needs_model(name):
    """Actions that themselves call the model, so they must be told which."""
    return name == "build_dashboard"


# Options for every agent-loop model call. num_ctx, because Ollama's own
# default context is small and an oversized prompt is silently truncated from
# the front - deleting the system prompt and its rules mid-conversation.
# Temperature low, because tool calls want precision, not flair: the Ollama
# default (~0.8) is where invented tool arguments come from.
AGENT_OPTIONS = {"temperature": 0.2, "num_ctx": OLLAMA_NUM_CTX}


def run_agent(client, model, engine, messages, allowed=None, on_call=None,
              confirm=None, max_steps=CHAT_MAX_STEPS):
    """Run one user turn to completion, executing tool calls as they come.

    Shared by the terminal chat and the web editor so both behave the same.

    allowed: restrict which tools the model may use. The web editor withholds
        reload_data because loading is a button the person presses there,
        exactly as it is in Qlik's own editor.
    on_call: called with (name, arguments) before each call, for display.
    confirm: called with a question for anything in DESTRUCTIVE; return False
        to refuse. Omit to refuse them outright.
    """

    tools = TOOLS
    if allowed is not None:
        tools = [t for t in TOOLS if t["function"]["name"] in allowed]

    # Category names the app itself reported this turn, so a paraphrased one
    # can be put back before the answer is shown.
    labels = set()

    for _ in range(max_steps):
        messages[:] = trim_history(messages)
        response = client.chat(
            model=model, messages=messages, tools=tools, options=AGENT_OPTIONS
        )
        message = response["message"]

        calls = message.get("tool_calls") or []
        messages.append({
            "role": "assistant",
            "content": message.get("content", ""),
            "tool_calls": calls,
        })

        if not calls:
            return snap_labels(message.get("content", "").strip(), labels)

        for call in calls:
            name = call["function"]["name"]
            arguments = call["function"].get("arguments") or {}
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {}

            if on_call:
                on_call(name, arguments)

            result = execute(
                engine, name, arguments, allowed=allowed, confirm=confirm
            )
            if name == "analyze_sheet":
                labels |= labels_in(result)

            messages.append({
                "role": "tool",
                "content": json.dumps(result, default=str)[:TOOL_RESULT_CHARS],
            })

    return "I stopped after too many steps. Could you narrow that down?"


# These conversations carry large tool results - one data model dump is
# thousands of characters. Past this budget the oldest turns are dropped,
# because overflowing the window fails the whole request, and losing the
# start of a conversation is a much smaller loss than that. The budget is
# derived from OLLAMA_NUM_CTX (see config.py) so a large-context model
# actually gets to use its memory - the old fixed 24,000 characters gave a
# 128k-context model about 6k tokens to work with.
MAX_HISTORY_CHARS = CHAT_HISTORY_CHARS

# Per-tool-result cap. Scales with the history budget: with real memory
# available, cutting a data model dump at 12k characters throws away columns
# the model was about to be asked about.
TOOL_RESULT_CHARS = max(12_000, MAX_HISTORY_CHARS // 8)


def trim_history(messages, max_chars=MAX_HISTORY_CHARS):
    """Drop the oldest turns once the conversation gets too big.

    Cuts only at user messages, never between an assistant's tool_calls and
    the tool results that answer them - a conversation split there is
    malformed and the model rejects it.
    """

    if not messages:
        return messages

    system = messages[:1] if messages[0].get("role") == "system" else []
    rest = messages[len(system):]

    def size(items):
        return sum(len(str(m.get("content") or "")) for m in items)

    if size(rest) <= max_chars:
        return list(messages)

    starts = [i for i, m in enumerate(rest) if m.get("role") == "user"]
    for cut in starts:
        if size(rest[cut:]) <= max_chars:
            return system + rest[cut:]

    # Even the latest turn is over budget; keep it anyway rather than send
    # nothing, and let the model's own limit decide.
    return system + (rest[starts[-1]:] if starts else rest[-2:])


def call_parts(call):
    """(name, arguments) from a tool call, dict or ollama object."""
    function = call["function"] if isinstance(call, dict) else call.function
    name = function["name"] if isinstance(function, dict) else function.name
    arguments = (
        function.get("arguments") if isinstance(function, dict) else function.arguments
    ) or {}

    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            arguments = {}

    return name, dict(arguments)


def stream_agent(client, model, engine, messages, allowed=None, confirm=None,
                 max_steps=CHAT_MAX_STEPS):
    """Run a turn, yielding events as they happen instead of at the end.

    A local model can spend minutes on a multi-step request. Returning only
    the finished answer makes that look like the software has hung, so the
    caller gets each tool call as it is decided and each token as it arrives.

    Events: {"type": "tool"|"tool_result"|"token"|"done"}.
    """

    tools = TOOLS
    if allowed is not None:
        tools = [t for t in TOOLS if t["function"]["name"] in allowed]

    labels = set()

    for _ in range(max_steps):
        content = []
        calls = []

        messages[:] = trim_history(messages)

        for chunk in client.chat(
            model=model, messages=messages, tools=tools, stream=True,
            options=AGENT_OPTIONS,
        ):
            message = chunk["message"]

            text = message.get("content")
            if text:
                content.append(text)
                yield {"type": "token", "text": text}

            calls.extend(message.get("tool_calls") or [])

        messages.append({
            "role": "assistant",
            "content": "".join(content),
            "tool_calls": calls,
        })

        if not calls:
            # The tokens already streamed are raw; the caller replaces them
            # with this reply, which is where the repair lands.
            yield {
                "type": "done",
                "reply": snap_labels("".join(content).strip(), labels),
            }
            return

        for call in calls:
            name, arguments = call_parts(call)
            yield {"type": "tool", "name": name, "arguments": arguments}

            result = execute(engine, name, arguments, allowed=allowed, confirm=confirm)
            if name == "analyze_sheet":
                labels |= labels_in(result)

            yield {
                "type": "tool_result",
                "name": name,
                "ok": not isinstance(result, dict) or "error" not in result,
                "detail": (result or {}).get("error") if isinstance(result, dict) else None,
            }

            messages.append({
                "role": "tool",
                "content": json.dumps(result, default=str)[:TOOL_RESULT_CHARS],
            })

    yield {"type": "done", "reply": "I stopped after too many steps. Could you narrow that down?"}


def execute(engine, name, arguments, allowed=None, confirm=None):
    """Run one action, refusing anything withheld or declined."""

    if allowed is not None and name not in allowed:
        return {"error": f"{name} is not available here."}

    function = FUNCTIONS.get(name)
    if function is None:
        return {"error": f"No such action {name!r}."}

    if name in DESTRUCTIVE:
        if confirm is None or not confirm(f"{name} replaces the data in this app. Run it?"):
            # Saying only "not allowed" makes a model conclude the action is
            # impossible and tell the user it cannot be done at all - which
            # is how a request to load data turns into a dead end.
            return {
                "cancelled": True,
                "note": (
                    "Not permitted right now - this is a setting, not a missing "
                    "capability. Tell the user to press the green 'Load data' "
                    "button, or to switch on 'assistant may load data', then "
                    "carry on from there."
                ),
            }

    try:
        return function(engine, **(arguments or {}))
    except TypeError as e:
        return {"error": f"Bad arguments for {name}: {e}"}
    except Exception as e:  # handed back so the model can correct itself
        return {"error": str(e)}


SYSTEM_PROMPT = f"""You manage a Qlik Sense app for someone who does not know \
Qlik. They describe what they want in plain language; you do the work with \
the tools and explain the result in plain language. Never show them Qlik \
syntax unless they ask.

How to work:

- FIRST, for any request about charts or data: call data_model. If it \
already shows the fields you need, the data is loaded - go on and build. \
Do NOT read or rewrite the load script when someone asks for a chart the \
loaded data already supports. Rewriting a working load script to answer \
"draw me some pie charts" destroys data that was already there.
- PLAN multi-step work before acting: what does the request need, what does \
the data model actually have, what is missing, which tools close the gap. \
Then work the plan one step at a time, checking each tool result before the \
next step.
- WHEN THE DATA IS NOT ENOUGH for what was asked - a grouping, trend or \
comparison that no loaded field supports - do not silently build something \
weaker, and do not just refuse. Say exactly what is missing, then propose \
how to get it: a column derived from existing fields (a date gives Year and \
Month for trends, a number gives bands or flags, an id gives counts), \
another file from the data sources, or a load script change. Ask the user \
first; once they agree, make the change, reload, and then build what they \
originally asked for.
- Only touch the load script when the user asks to load, add, merge, clean \
or change data - or when they have just agreed to a script change you \
proposed because the data was not enough. Otherwise leave it alone.
- Loading data: use data_sources to see what exists. If they name a folder \
that is not a connection yet, add_data_source it first. Preview every file \
with data_sources before writing any LOAD statement, so the script uses the \
file's real column names. Use build_load_script to generate the script, \
write_script to apply it, then reload_data.
- "Merge" or "combine" files means build_load_script with mode='concatenate'.
- Cleaning: reload first, then data_model shows which columns are constant, \
mostly empty, or duplicated. Then regenerate the script with drop_fields for \
the useless columns and null_tokens for placeholders like 'N/A', and reload \
again. Explain what you dropped and why.
- A chart needing more than one dimension or measure - sankey, scatter, \nmekko, grid, a combo chart with two measures - must be built with \ncreate_chart. build_dashboard cannot express it and will quietly build \nsomething else instead. If the user names the fields, use exactly those.\n
- Building dashboards: for a broad brief ("make me a sales dashboard"), \
call build_dashboard and pass the user's request through in their own words \
- it designs from the real field list, with distinct counts and sample \
values. When the user is specific - named fields, named chart types, \
particular comparisons - design the charts yourself with create_chart, one \
call per chart, checking anything you are unsure of with query or \
check_expression first. Report back what was built and anything that was \
skipped.
- Measures: simple aggregations - Sum([Field]), Count([Field]), \
Count(DISTINCT [Field]), Avg, Min, Max - are right for most charts. Richer \
expressions are allowed in create_chart and edit_chart: set analysis such \
as Sum({{<[Year]={{'2024'}}>}} [Sales]), or arithmetic such as \
Sum([Profit])/Sum([Sales]). Qlik checks them and rejects what is wrong, so \
try check_expression when unsure. Never put SortBy or Limit inside an \
expression - it silently fails to calculate.
- For a "top 5", use the chart's limit argument, or query with limit=5 to \
see the values; a chart cannot rank inside its expression.

AVAILABLE CHART TYPES (name, then how many dimensions and measures it takes):
{describe_chart_types()}

Everyday names work too - "scatter", "pivot table", "combo", "funnel", "word cloud", "box plot". If someone asks for a chart type in that list, you CAN build it: say yes and build it. Only say a chart is unavailable if it is genuinely not in the list.

Rules:

- For a plain file load, call build_load_script to produce the script - it \
gets quoting, trimming and nulls right - then write_script to apply it. To \
drop columns, pass drop_fields. Do not hand-write what build_load_script \
can generate.
- For transformations build_load_script cannot express - joins between \
tables, RESIDENT loads, GROUP BY aggregation, mapping tables, a master \
calendar - you MAY write the Qlik script yourself and apply it with \
write_script into your own tab. Call read_script first so you build on what \
is there, and preview files before referencing their columns. The script is \
syntax-checked and rolled back if it does not parse, so a mistake is \
reported rather than breaking the app. Never rewrite or delete a tab you \
did not create - add your work in its own tab, and reload to verify it.
- TO ADD A NEW OR CALCULATED COLUMN, pass `derived` to build_load_script: \
[{{"name": "Year", "expression": "Year([Report Date])"}}]. You ARE able to do \
this - never tell the user that adding a calculated field is impossible or \
not permitted. Individual expressions are yours to write; only the statement \
around them is generated. If the expression is wrong the syntax check \
rejects it and you can correct it.
- When the user asks for derived columns and leaves the choice to you, \
choose sensible ones from the fields that exist and say what you picked, \
rather than asking them to specify. A date field gives Year and Month; a \
numeric field gives a band or a flag.
- NEVER call reload_data unless the user has asked you to load or reload \
data, or has just agreed to a script change that needs a reload to take \
effect. It replaces every row in the app. If they asked to see or plan \
something, stop when you have shown it.
- Never invent a field, file or connection name. If you are unsure what \
exists, look it up first.
- Report ONLY what a tool actually returned. build_dashboard returns a \
"built" list and a "skipped" list: describe those exactly, using the chart \
types in "built". Never describe a chart that is not in "built", never \
invent a chart type, and give the real counts - "7 built, 1 skipped", not \
"8 charts" followed by a list of eight. The only chart types that exist are \
listed in AVAILABLE CHART TYPES below - use those exact names, and do not invent one that is not there.
- A SKIPPED chart was NOT created and is NOT in the app. Never say it was \
saved, and never count it towards what you produced. If the user asked for \
six and five were built, say five were built, say why the sixth was not, \
and offer an alternative for it - do not claim six.
- If a tool reports skipped charts or an error, say so plainly and fix it.
- Keep the build report short: say what you did and what the result was.

WHAT THE NUMBERS MEAN

The people using this read balance sheets, not data models. A chart nobody can interpret was not worth building, so once you have built one, tell them what it shows.

- After a successful build_dashboard or create_chart, call analyze_sheet and then explain the result. Building a chart shows you none of its values; analyze_sheet reads them out of the live app and does the arithmetic.
- Never work out a percentage, share, growth rate or total yourself. Not from query rows, not from figures earlier in the conversation, not in your head. `query` returns rows; turning them into "68% of the book" is arithmetic, and arithmetic done in a reply is where the wrong decimal comes from. analyze_sheet does that sum against the live app - call it and quote what it returns. If the figure you need is not in what it returned, say so instead of producing one.
- Use ONLY figures analyze_sheet returned. Never calculate a number it did not give you, never estimate one, and never turn its percentage into a different one. If it says a measure is not additive, do not state a share or a total for it. A confident wrong number in front of a banker costs far more than a short answer.
- Read it the way an analyst would: what is largest and smallest, how much of the total sits in the top few, which way a trend moved and by how much, what deserves a second look. Give the business meaning, not the chart mechanics - "three regions hold 71% of deposits" rather than "the bar chart is sorted descending".
- Two or three sentences for a sheet. Point at what matters instead of walking through every chart in turn.
- Category labels from analyze_sheet are data, not prose. Quote them character-for-character - never translate, shorten, expand or tidy one. Renaming a deposit category or a region in the summary is the same error as inventing a number, and the reader is the person who will notice.
- Say plainly when the data cannot answer something, rather than reaching for the nearest number that happens to be available.
- Never explain in Qlik terms. No expressions, no field syntax, no dimensions or hypercubes, unless they ask.

LANGUAGE

- Reply in the language the user wrote to you in, and stay in it for the whole answer including the explanation of the numbers.
- Never translate identifiers. Field names, table names, tab names, connection names, chart types and Qlik expressions are used exactly as they appear in the app, in every language - a translated field name builds a chart that renders empty. Translate the sentence around it, not the name inside it."""


# The languages the interface offers. Pinning one is not the same as the
# model guessing from the question: a banker who types a field name in
# English inside an Uzbek sentence should not flip the answer to English.
LANGUAGE_NAMES = {"en": "English", "ru": "Russian", "uz": "Uzbek"}


def system_prompt(language=""):
    """The system prompt, with the glossary in front and the language pinned.

    Read per conversation rather than at import, so editing the glossary and
    starting a new chat is enough - no restart. The rules come after the
    glossary, so a glossary cannot loosen them.
    """
    prompt = glossary.prompt_section() + SYSTEM_PROMPT

    name = LANGUAGE_NAMES.get((language or "").strip().lower())
    if name:
        prompt += (
            f"\n\nANSWER IN {name.upper()}\n\n"
            f"- The reader has set this interface to {name}. Write every "
            f"answer in {name} - whatever language the question is typed in, "
            "and whatever language the values in the data happen to be in.\n"
            "- This does not loosen the rule above: field names, table "
            "names, chart types and category labels read out of the app keep "
            "their exact spelling and are never translated."
        )
    return prompt
