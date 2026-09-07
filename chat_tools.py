import json
import logging
from collections import Counter

from chart_specs import (
    AVAILABLE_TYPES,
    CHART_TYPES,
    DIMENSIONAL_TYPES,
    available,
    chart_requirements,
    describe_chart_types,
    fallback_types,
    resolve_chart_type,
)
from config import CHAT_HISTORY_CHARS, CHAT_MAX_STEPS, MODEL_NUM_CTX
from qlik_engine import QlikEngineError, QlikNotFoundError
from dashboard_builder import build_sheet as _build_sheet
from dashboard_builder import (
    design_full_dashboard,
    enrich_fields,
    probe_visualization,
)
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

DESTRUCTIVE = {"reload_data", "delete_sheet"}

LOAD_EDITOR_TOOLS = {
    "data_sources", "add_data_source", "read_script",
    "build_load_script", "write_script", "data_model", "query",
}

DATA_FILE_SUFFIXES = (
    ".csv", ".txt", ".tab", ".qvd", ".xlsx", ".xls", ".json", ".xml", ".parquet",
)


def open_app(engine, app=""):
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
    return engine.create_connection(name, folder_path)


def read_script(engine, tab=""):
    script = engine.get_script()
    names = tab_names(script)

    if tab:
        body = get_tab(script, tab)
        if body is None:
            return {"error": f"No tab {tab!r}. Tabs: {', '.join(names)}"}
        return {"tabs": names, "tab": tab, "content": body}

    return {"tabs": names, "content": script}


def write_script(engine, content="", tab=GENERATED_TAB, mode="replace_tab"):
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
        return {"error": "mode must be replace_tab, append, replace_all or delete_tab"}

    engine.set_script(updated)
    return {"ok": True, "tabs": tab_names(updated), "note": "Not reloaded yet."}


def build_load_script(engine, sources, mode="separate", drop_fields=None,
                      trim_text=True, null_tokens=None, table_name="",
                      derived=None):
    previewed = []
    for source in sources or []:
        preview = engine.preview_file(source["connection"], source["path"], sample_rows=20)
        for table in preview["tables"]:
            previewed.append({
                "connection": source["connection"],
                "path": source["path"],
                "table": source.get("table") or table["name"],
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
    result = engine.reload_data()
    if result.get("success"):
        result["tables"] = [
            {"name": t["name"], "rows": t["rows"]} for t in engine.get_tables()
        ]
    return result


def data_model(engine):
    return describe_data_model(engine)


def query(engine, dimensions=None, measures=None, limit=20):
    result = engine.query(
        dimensions=dimensions or [], measures=measures or [], limit=limit
    )
    return add_shares(result=result, engine=engine,
                      dimensions=dimensions, measures=measures)


def build_dashboard(engine, instruction, title="", model=None):
    fields = engine.get_fields()
    if not fields:
        return {"error": "No data is loaded in this app. Load data first."}

    if model is None:
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


def _chart_problem(engine, chart_type, title, dimensions, measures):
    resolved = resolve_chart_type(chart_type)
    if resolved is None:
        return (
            f"{chart_type!r} is not a chart type. Use one of: "
            f"{', '.join(CHART_TYPES)}."
        )

    if not (title or "").strip():
        return "every chart needs a title - it is what the user reads first."

    if not available(resolved):
        return (
            f"{resolved} is not installed on this Qlik server - it would show "
            f"'Invalid visualization' on the sheet instead of data. This server "
            f"has: {', '.join(sorted(AVAILABLE_TYPES))}."
        )

    dimensions = [d for d in (dimensions or []) if str(d).strip()]
    measures = [m for m in (measures or []) if str(m).strip()]

    (min_dims, max_dims), (min_meas, max_meas) = chart_requirements(resolved)

    if (min_dims or resolved in DIMENSIONAL_TYPES) and not dimensions:
        return (
            f"{resolved} needs a dimension to group by and none was given. "
            f"Without one it draws a single undifferentiated bar rather than "
            f"the breakdown the user asked for."
        )

    if len(dimensions) < min_dims:
        return (
            f"{resolved} needs at least {min_dims} dimension(s) to group by "
            f"and you gave {len(dimensions)}. Without it the chart renders "
            f"blank."
        )
    if len(dimensions) > max_dims:
        return (
            f"{resolved} takes at most {max_dims} dimension(s) and you gave "
            f"{len(dimensions)}."
        )
    if len(measures) < min_meas:
        return (
            f"{resolved} needs at least {min_meas} measure(s) - an expression "
            f"such as Sum([Field]) - and you gave {len(measures)}. Without "
            f"one there is nothing to plot and the chart renders blank."
        )
    if len(measures) > max_meas:
        return (
            f"{resolved} takes at most {max_meas} measure(s) and you gave "
            f"{len(measures)}."
        )

    try:
        known = {f["name"] for f in engine.get_fields()}
    except Exception:
        known = None
    if known:
        unknown = [d for d in dimensions if d not in known]
        if unknown:
            return (
                f"these are not fields in this app: "
                f"{', '.join(map(repr, unknown))}. Check the spelling against "
                f"data_model - the app has: {', '.join(sorted(known))}."
            )

    for measure in measures:
        try:
            verdict = engine.check_expression(measure)
        except Exception:
            continue
        if not verdict.get("valid"):
            reason = verdict.get("error") or "invalid expression"
            bad = verdict.get("bad_fields") or []
            extra = f" Unknown field(s): {', '.join(bad)}." if bad else ""
            return f"the measure {measure!r} is not valid: {reason}.{extra}"

    return None


def _chart_preview(engine, chart_type, dimensions, measures):
    return probe_visualization(
        engine,
        {
            "type": resolve_chart_type(chart_type),
            "dimensions": [d for d in (dimensions or []) if str(d).strip()],
            "measure_expressions": [
                m for m in (measures or []) if str(m).strip()
            ],
        },
    )


def _build_verified(engine, chart_type, title, dimensions, measures,
                    color=None, limit=None):
    attempts = [resolve_chart_type(chart_type) or chart_type]
    attempts += [
        t for t in fallback_types(attempts[0], dimensions, measures)
        if t not in attempts
    ]

    failures = []
    for index, candidate in enumerate(attempts):
        try:
            response = engine.create_chart(
                candidate, title,
                dimensions=list(dimensions or []),
                measure_expressions=list(measures or []),
                colour=color, limit=limit,
            )
        except QlikEngineError as e:
            failures.append(f"{candidate} was rejected ({e})")
            continue

        object_id = (
            response.get("result", {}).get("qReturn", {}).get("qGenericId")
            if isinstance(response, dict) else None
        )
        if not object_id:
            return candidate, None

        ok, detail = engine.chart_renders(object_id)
        if ok:
            if index == 0:
                return candidate, None
            why = failures[0] if failures else "it did not draw"
            return candidate, (
                f"{attempts[0]} was asked for but would not draw ({why}), "
                f"so this is a {candidate} over the same dimensions and "
                f"measures"
            )

        failures.append(f"{candidate}: {detail}")
        engine.delete_chart(object_id)

    return None, "; ".join(failures)


def create_chart(engine, chart_type, title, dimensions=None, measures=None,
                 sheet_title=None, color=None, limit=None):
    problem = _chart_problem(engine, chart_type, title, dimensions, measures)
    if not problem:
        preview, problem = _chart_preview(engine, chart_type, dimensions, measures)

    if problem:
        return {
            "error": f"Not created - it would render blank: {problem}",
            "created": False,
        }

    if sheet_title:
        try:
            engine.open_sheet(sheet_title)
        except QlikNotFoundError:
            engine.create_sheet(sheet_title)
    elif engine.sheet_handle is None:
        engine.create_sheet(title)

    built, substituted = _build_verified(
        engine, chart_type, title, dimensions, measures, color, limit
    )
    engine.save()

    if not built:
        return {
            "error": (
                f"Built it and it would not draw: {substituted}. Nothing was "
                f"left on the sheet. The data is fine - the chart type is "
                f"the problem, so ask for a different one."
            ),
            "created": False,
        }

    created = {"type": built, "title": title}
    if substituted:
        created["substituted"] = substituted
    if preview:
        created["shows"] = preview

    return {
        "created": created,
        "sheet": sheet_title or engine.sheet_id,
        "saved": True,
    }


def list_charts(engine):
    charts = engine.list_charts()
    counts = Counter(chart.get("sheet") for chart in charts)
    return {
        "charts": charts,
        "sheets": [
            {"title": sheet["title"], "charts": counts.get(sheet["title"], 0)}
            for sheet in engine.list_sheets()
        ],
    }


def analyze_sheet(engine, sheet="", chart_ids=None):
    return analyse_sheet(engine, sheet=sheet or None, chart_ids=chart_ids or None)


def edit_chart(engine, chart_id, title=None, measure_expression=None,
               measure_label=None, dimension=None, color=None, limit=None):
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
    return engine.check_expression(expression)


def delete_sheet(engine, sheet):
    return engine.delete_sheet(sheet)


def save(engine):
    engine.save()
    return {"saved": True}


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
        "Build ONE sheet of charts and save it. Every call creates a NEW "
        "sheet, so calling it twenty times makes twenty sheets - if all the "
        "charts belong together on ONE sheet, make ONE call and ask for that "
        "many charts in the instruction (\"one sheet with 20 charts covering "
        "...\"). The word \"dashboards\" being plural does not mean call "
        "this more than once - without an explicit count from the user, "
        "make exactly ONE call. Any chart type in AVAILABLE CHART "
        "TYPES can be used - say which you want in the instruction, e.g. "
        "\"a gauge of total sales and a treemap by region\". Pass the user's "
        "request through "
        "as `instruction`, in their own words - including any number of "
        "charts they asked for and any fields they named. The charts are "
        "designed from the app's real data, validated, and anything that "
        "would render blank is skipped. Do not try to specify the charts "
        "yourself; describe what is wanted and let this design it. Each "
        "built chart returns a \"shows\" block with what it actually "
        "contains when queried - read it and report from it.",
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
        "measure expressions, plus every sheet with how many charts is on "
        "it. Use the \"sheets\" list for sheet counts, not the charts: a "
        "sheet with nothing on it appears there with 0 charts and is worth "
        "telling the user about. The id is the only way to identify a chart "
        "for editing - get it from here before calling edit_chart. Returns "
        "NO DATA VALUES; use query for the actual numbers.",
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
    _tool(
        "delete_sheet",
        "Remove a sheet and EVERY chart on it. Destructive and not undoable: "
        "the charts go with the sheet. Use it to undo sheets that were built "
        "by mistake, and name the sheet exactly as list_charts reports it - "
        "or pass its id when two sheets share a title. Deleting one sheet is "
        "one call: there is no way to remove several at once, and no way to "
        "get one back.",
        {"sheet": _STRING},
        ["sheet"],
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
    "delete_sheet": delete_sheet,
    "save": save,
}

assert {t["function"]["name"] for t in TOOLS} == set(FUNCTIONS), "tool list drifted"


def _needs_model(name):
    return name == "build_dashboard"


AGENT_OPTIONS = {"temperature": 0.2, "num_ctx": MODEL_NUM_CTX}


def _call_raw(call):
    function = call["function"] if isinstance(call, dict) else call.function
    name = function["name"] if isinstance(function, dict) else function.name
    raw = (
        function.get("arguments") if isinstance(function, dict) else function.arguments
    )
    return name, raw


def _plain_calls(calls):
    plain = []
    for call in calls:
        name, raw = _call_raw(call)
        arguments = raw if isinstance(raw, str) else dict(raw or {})
        plain.append({"function": {"name": name, "arguments": arguments}})
    return plain


def _parse_arguments(name, raw):
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as e:
            return {}, (
                f"The arguments for {name} were not valid JSON ({e}), so it "
                f"was not run. Repeat the call with well-formed JSON "
                f"arguments."
            )
    return dict(raw or {}), None


def run_agent(client, model, engine, messages, allowed=None, on_call=None,
              confirm=None, max_steps=CHAT_MAX_STEPS):
    tools = TOOLS
    if allowed is not None:
        tools = [t for t in TOOLS if t["function"]["name"] in allowed]

    labels = set()

    for _ in range(max_steps):
        messages[:] = trim_history(messages)
        response = client.chat(
            model=model, messages=messages, tools=tools, options=AGENT_OPTIONS
        )
        message = response["message"]

        calls = _plain_calls(message.get("tool_calls") or [])
        messages.append({
            "role": "assistant",
            "content": message.get("content", ""),
            "tool_calls": calls,
        })

        if not calls:
            return snap_labels(message.get("content", "").strip(), labels)

        for call in calls:
            name, raw = _call_raw(call)
            arguments, argument_error = _parse_arguments(name, raw)

            if on_call:
                on_call(name, arguments)

            if argument_error:
                result = {"error": argument_error}
            else:
                result = execute(
                    engine, name, arguments, allowed=allowed, confirm=confirm
                )
            if name == "analyze_sheet":
                labels |= labels_in(result)

            messages.append(_tool_message(name, result))

    return "I stopped after too many steps. Could you narrow that down?"


MAX_HISTORY_CHARS = CHAT_HISTORY_CHARS

TOOL_RESULT_CHARS = max(12_000, MAX_HISTORY_CHARS // 8)


def _tool_message(name, result):
    payload = json.dumps(result, default=str)
    if len(payload) > TOOL_RESULT_CHARS:
        payload = payload[:TOOL_RESULT_CHARS] + "...[truncated]"
    return {"role": "tool", "tool_name": name, "content": payload}


def trim_history(messages, max_chars=MAX_HISTORY_CHARS):
    if not messages:
        return messages

    system = messages[:1] if messages[0].get("role") == "system" else []
    rest = messages[len(system):]

    def size(items):
        total = 0
        for m in items:
            total += len(str(m.get("content") or ""))
            if m.get("tool_calls"):
                total += len(json.dumps(m["tool_calls"], default=str))
        return total

    if size(rest) <= max_chars:
        return list(messages)

    starts = [i for i, m in enumerate(rest) if m.get("role") == "user"]
    for cut in starts:
        if size(rest[cut:]) <= max_chars:
            return system + rest[cut:]

    if starts:
        return system + rest[starts[-1]:]

    cut = max(len(rest) - 2, 0)
    while cut > 0 and rest[cut].get("role") == "tool":
        cut -= 1
    return system + rest[cut:]


def call_parts(call):
    name, raw = _call_raw(call)
    arguments, _ = _parse_arguments(name, raw)
    return name, arguments


STOPPED_NOTE = "The user stopped this turn before the action ran."


def close_open_tool_calls(messages, note=STOPPED_NOTE):
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.get("role") != "assistant":
            continue

        calls = message.get("tool_calls") or []
        if not calls:
            return 0

        answered = sum(
            1 for m in messages[index + 1:] if m.get("role") == "tool"
        )
        unanswered = calls[answered:]
        for call in unanswered:
            name = (call.get("function") or {}).get("name") or "unknown"
            messages.append(_tool_message(name, {"stopped": note}))
        return len(unanswered)

    return 0


def stream_agent(client, model, engine, messages, allowed=None, confirm=None,
                 max_steps=CHAT_MAX_STEPS):
    tools = TOOLS
    if allowed is not None:
        tools = [t for t in TOOLS if t["function"]["name"] in allowed]

    labels = set()

    try:
        yield from _stream_steps(
            client, model, engine, messages, tools, allowed, confirm,
            max_steps, labels,
        )
    finally:
        close_open_tool_calls(messages)


def _stream_steps(client, model, engine, messages, tools, allowed, confirm,
                  max_steps, labels):
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

            calls.extend(_plain_calls(message.get("tool_calls") or []))

        messages.append({
            "role": "assistant",
            "content": "".join(content),
            "tool_calls": calls,
        })

        if not calls:
            yield {
                "type": "done",
                "reply": snap_labels("".join(content).strip(), labels),
            }
            return

        for call in calls:
            name, raw = _call_raw(call)
            arguments, argument_error = _parse_arguments(name, raw)
            yield {"type": "tool", "name": name, "arguments": arguments}

            if argument_error:
                result = {"error": argument_error}
            else:
                result = execute(engine, name, arguments, allowed=allowed, confirm=confirm)
            if name == "analyze_sheet":
                labels |= labels_in(result)

            yield {
                "type": "tool_result",
                "name": name,
                "ok": not isinstance(result, dict) or "error" not in result,
                "detail": (result or {}).get("error") if isinstance(result, dict) else None,
            }

            messages.append(_tool_message(name, result))

    yield {"type": "done", "reply": "I stopped after too many steps. Could you narrow that down?"}


def execute(engine, name, arguments, allowed=None, confirm=None):
    function = FUNCTIONS.get(name)

    if allowed is not None and name not in allowed:
        withheld = {"error": f"{name} is not available here."}
        if function is not None:
            withheld["note"] = (
                f"{name} exists but is switched off on this surface. It is "
                f"a setting, not a missing capability: say it is turned off "
                f"here, say what it would have done, and offer what the "
                f"user can do instead. Do not say you are unable to do it."
            )
        return withheld

    if function is None:
        return {"error": f"No such action {name!r}."}

    if name == "delete_sheet":
        question = (
            f"delete_sheet removes the sheet {(arguments or {}).get('sheet')!r} "
            "and every chart on it. Run it?"
        )
        note = (
            "Not permitted right now - this is a setting, not a missing "
            "capability. You CAN delete sheets; this one was declined. Say "
            "which sheet you were about to delete, and that they can remove "
            "it themselves in Qlik or approve the deletion here."
        )
    elif name in DESTRUCTIVE:
        question = f"{name} replaces the data in this app. Run it?"
        note = (
            "Not permitted right now - this is a setting, not a missing "
            "capability. Tell the user to run the reload themselves - in "
            "the web editor that is the 'Load data' button - or to switch "
            "on 'assistant may load data', then carry on from there."
        )
    elif name == "write_script" and (arguments or {}).get("mode") == "replace_all":
        question = (
            "write_script with mode='replace_all' overwrites the whole load "
            "script. Run it?"
        )
        note = (
            "Not permitted right now - this is a setting, not a missing "
            "capability. Replacing the whole load script needs the user's "
            "go-ahead. Use mode='replace_tab' to change only your own tab, "
            "or ask the user to approve the full rewrite, then carry on."
        )
    else:
        question = note = None

    if question is not None:
        if confirm is None or not confirm(question, name):
            return {"cancelled": True, "note": note}

    try:
        return function(engine, **(arguments or {}))
    except TypeError as e:
        return {"error": f"Bad arguments for {name}: {e}"}
    except Exception as e:
        return {"error": str(e)}


SYSTEM_PROMPT = f"""You manage a Qlik Sense app for a business person - a \
banker, a manager, an analyst - who does not know Qlik, does not know what \
tools you have, and will not describe what they want precisely. They write \
four words and expect you to work it out.

Your job: work out what they meant, do it with the tools, and report back in \
plain business language. Never show them Qlik syntax, field names in \
brackets, tool names, or JSON unless they ask for it.

Answer in the same language the user wrote in.

=====================================================================
1. EVERY TURN, IN ORDER
=====================================================================

STEP 1 - Decide which intent below the request is. If two fit, choose the \
one that only READS. Reading is always safe; building and loading are not.
STEP 2 - Run that intent's tool sequence to the END. If the request had two \
parts, do both parts before answering. Never answer a question about this \
app from memory or assumption: if you have not called a tool this turn, you \
do not yet know the answer. A question about charts or data is not answered \
until you have the NUMBERS as well as the names - one listing call is the \
start of the work, never the end of it.
STEP 3 - Before you write the answer, re-read the last tool result and check \
that every item in it appears in what you are about to say. Then report \
using the answer contract in section 5.

=====================================================================
2. INTENT ROUTING - match the request to ONE row
=====================================================================

A. INVENTORY - "what do I have?"
   Sounds like: "info about my dashboards", "info dashboards", "what \
dashboards do I have", "what's in this app", "show me my reports", "what \
data is here", "list the sheets", "anything built already?"
   This is a QUESTION, NOT a build request. Build nothing.

B. EXPLAIN - "what does this one show?"
   Sounds like: "what is the sales sheet", "explain that chart", "what does \
the KPI mean", "where does this number come from".

C. NUMBER - "what is the figure?"
   Sounds like: "total sales", "how many customers", "sales by region", \
"top 5 branches", "which region is biggest", "compare with last year".
   Answer with the number in the chat. Do NOT build a chart unless asked.

D. BUILD - "make me something"
   Sounds like: "build a dashboard", "make a sales dashboard", "draw me a \
pie chart", "show sales by region as a bar chart", "I need a report on \
deposits", "visualise this".

E. EDIT - "change what exists"
   Sounds like: "make it red", "rename that", "wrong number", "show only \
top 10", "group it by branch instead".

F. DATA - "get the numbers in"
   Sounds like: "load this file", "add my downloads folder", "merge these \
spreadsheets", "clean the data", "refresh", "add a year column".

G. CHAT - greetings and thanks, nothing more.
   Answer directly in one or two sentences. Call no tools.

I. DELETE - "get rid of it"
   Sounds like: "delete that sheet", "remove the dashboard", "delete all \
the sheets", "clear this app", "start over".
   You CAN do this - delete_sheet is yours. Never answer that you cannot.

H. CAPABILITY - "what can YOU do?"
   Sounds like: "what can you do", "what can you build", "what charts can \
you draw", "how many chart types do you have", "what kinds of dashboards \
can you make", "what types are available", "can you do a sankey".
   This asks about YOU, not about their app. Do not call list_charts - \
their app's contents are a different question (intent A).
   Watch for the confusion: "how many dashboards can you draw" is this \
intent, and "how many dashboards do I have" is intent A. If a request could \
be either, answer THIS one and offer the other in a single closing line - \
they are quick to answer and the user should never have to ask twice.

Ambiguous words, resolved:
- "dashboard", "report", "sheet", "page" all mean the same thing to this \
user. Ask which only if it changes what you would do.
- "show me X" is intent C (a number) when X is a quantity, and intent D (a \
chart) when they name a chart type or say chart, graph or visual.
- "info", "information", "details", "about" almost always mean intent A or \
B. They almost never mean build.

=====================================================================
3. WHAT TO CALL, PER INTENT
=====================================================================

A. INVENTORY - they want to KNOW what they have, WITH THE NUMBERS
   Listing titles is NOT an answer. list_charts returns titles, types and \
expressions - it returns NO DATA. A report built only from it tells the user \
nothing they could not see by looking at their own screen. You must go and \
fetch the numbers.
   1. list_charts - every chart, its type, title, dimensions, measures and \
which sheet it is on, PLUS a "sheets" list with every sheet and its chart \
count. Take your sheet count from "sheets", never from the charts: a sheet \
with nothing on it owns no charts and would otherwise be missing from your \
answer entirely. An empty sheet is worth a line of its own.
   2. Work out the DISTINCT dimension-and-measure pairs across those charts. \
Charts repeat: a bar chart, a treemap, a boxplot and a waterfall of the same \
field all need the SAME query. Twenty-eight charts are usually six to ten \
distinct queries.
   3. query each distinct pair ONCE, with a limit of 5 to 10, plus each KPI \
measure on its own for the headline totals. This is the step that turns a \
list into a briefing - do not skip it.
   3a. IF ANY CHART IS OVER TIME - a line chart, a trend, anything grouped \
by a date - you MUST query that date field as a dimension, with a limit of \
at least 12. Without it you cannot say whether the business is going up or \
down, which is the first thing anyone asks. Report the first period, the \
last period, the change between them as a percentage, and the highest and \
lowest periods. "Shows the trend over time" is not a report of a trend.
   3b. IF ANY CHART COUNTS RATHER THAN SUMS - a record count, a frequency, \
an average - query that too: Count([Field]) and Avg([Field]) are separate \
numbers from Sum([Field]) and you cannot derive one from another.
   4. If there are more than 12 distinct pairs, query the 12 that cover the \
most charts, and say at the end which ones you did not pull numbers for. \
Never pretend you covered them all.
   5. data_model - only if they also asked what data is in there.

   6. NOW REPORT EVERY CHART SEPARATELY. Steps 2-4 deduplicate the QUERIES \
you run. They do NOT deduplicate the ANSWER. Every chart on every sheet gets \
its OWN numbered row, carrying its OWN numbers - including the ten charts \
that show the same field ten ways. You already have those numbers from one \
query; write them out again on each row that needs them, and add a short \
note saying which earlier chart it repeats. A sheet with 28 charts produces \
28 rows. Count them before you answer.
   NEVER collapse charts into a group line. "Breakdowns (bars, pies, \
treemaps): SUM by Region, Agent, Deposit Type and Currency" is exactly the \
failure this rule exists to prevent - it takes six charts the user asked \
about and returns one sentence with no figures in it.
   A chart whose definition has no measure, no dimension, or an empty title \
gets a row too, saying plainly that it is broken and renders nothing.
   EVERY ROW MUST CARRY AT LEAST ONE NUMBER. Only three kinds of row are \
allowed without one: a filter pane, a broken chart, and a chart whose data \
you did not query - and that last one must say "not queried" in the row, in \
those words. "Shows the time series", "Averages the amounts over time" and \
"Cross-references region against agent" are descriptions of a chart's \
purpose, not reports of its contents, and they are what this rule exists to \
stop. If you find yourself writing a row like that, go and run the query.
   Number the rows from 1 within each sheet, and check that the last number \
on a sheet equals that sheet's chart count before you send the answer.
   WHEN THE APP IS GENUINELY TOO BIG for one reply - more than about 40 \
charts - you may expand fewer sheets, but NEVER by vagueness. Give every \
sheet by name with its EXACT chart count, expand the largest or most \
relevant few in full, then name precisely which sheets you have not expanded \
and offer to go through them next. "Over 60 charts", "the list was quite \
long", "and various others" are all failures; "12 sheets, 63 charts - I have \
listed the 3 largest below, say the word for Risk Management, Channel \
Performance or the other 6" is correct. An exact count and named remainder \
is never too long.

   Then write the briefing described in section 5.
   A chart that is empty, untitled, duplicated on another sheet, or whose \
query comes back as zero or no rows is IMPORTANT NEWS. Say so plainly and \
name it - a broken chart the user believes is working is worse than no \
chart. If the app has no charts at all, say so and offer to build one - do \
not build it unasked.

B. EXPLAIN
   1. list_charts, and find the one they mean by its title.
   2. query it - ALWAYS. Explaining a chart without its current values is \
describing a picture the user is already looking at.
   Translate the measure into business language: a sum of amounts grouped by \
region is "the total amount, broken down by region". Never read the \
expression out to them.

C. NUMBER
   1. data_model - confirm the fields exist and see their real names.
   2. query - dimensions plus measures, limit for a top N.
   Give the answer as a short sentence and, when there is more than one row, \
a small markdown table. Then offer: "Want this as a chart?"

D. BUILD
   1. data_model FIRST, always. If it already shows the fields you need, the \
data is loaded - go on and build. Do NOT read or rewrite the load script \
when someone asks for a chart the loaded data already supports.
   2. Broad brief ("a sales dashboard", "something about deposits") -> \
build_dashboard, passing the user's words through as the instruction, \
including any number of charts they asked for.
   2a. Plural "dashboards" alone is NOT a request for several sheets: \
"make me some dashboards" is ONE call, one sheet, with a fuller set of \
charts. Call build_dashboard more than once only when the user gives a \
count ("4 dashboards") or asks for separate sheets.
   3. Specific brief - they named the fields, the chart type, or the exact \
comparison -> create_chart, one call per chart. Check anything you are \
unsure of with query or check_expression first.
   4. A chart needing more than one dimension or measure - sankey, scatter, \
mekko, grid, a combo chart with two measures - must be built with \
create_chart. build_dashboard cannot express it and will quietly build \
something else instead. If the user names the fields, use exactly those.
   5. Report what was built, honestly (section 6).

E. EDIT
   1. list_charts - the id is the only way to identify a chart.
   2. edit_chart with only the arguments that change.
   Never rebuild a whole sheet to change one chart.

I. DELETE
   1. list_charts FIRST - name the sheets exactly and count what is on them.
   2. Say what will go ("3 sheets, 41 charts") before deleting anything.
   3. delete_sheet, ONE CALL PER SHEET. There is no bulk delete.
   4. COUNT THE RESULTS, NOT THE CALLS. A sheet is gone only if that call \
returned "deleted". A call returning "error" or "cancelled" removed \
NOTHING. Ten calls that all errored is ZERO deleted - report that, say why, \
and never total up calls you made instead of sheets that went.
   5. Say "all" only if you deleted every sheet list_charts reported. \
Otherwise give both numbers: "deleted 10 of 27".

F. DATA
   1. data_sources to see what connections exist. If they name a folder that \
is not a connection yet, add_data_source it first.
   2. Preview every file with data_sources before writing any LOAD \
statement, so the script uses the file's real column names.
   3. build_load_script to generate it, write_script to apply it, then \
reload_data.
   "Merge" or "combine" files means build_load_script with \
mode='concatenate'.
   Cleaning: reload first, then data_model shows which columns are constant, \
mostly empty or duplicated; regenerate the script with drop_fields for the \
useless columns and null_tokens for placeholders like 'N/A', reload again, \
and explain what you dropped and why.

H. CAPABILITY
   Call NO tools. Everything you need is in AVAILABLE CHART TYPES below.
   1. Give the real count, then list the types themselves - ALL of them, \
grouped by what they are for, using the everyday name with a few words on \
when to use it. Work down AVAILABLE CHART TYPES entry by entry rather than \
recalling them: the number of types you list must equal the number you \
quoted. Listing twenty and claiming twenty-three is the failure to avoid, \
and the ones that go missing are the unglamorous ones - the waterfall and \
the org chart, not the sankey.
   Say what each one is FOR, and nothing more. Do not attach features to it: \
a pivot table is "the same as in Excel", NOT "with drill-down" - there is no \
drill-down here, and inventing one small feature per chart is how a whole \
capability gets promised.
   2. THERE ARE NO DASHBOARD TEMPLATES OR TYPES. A dashboard here is a \
sheet with charts on it, built to order from the list. Asked what KINDS you \
can make, say exactly that, then offer two or three examples you could \
assemble from their actual fields - as suggestions, not presets.
   3. Answer only from that list. If a type is not on it you cannot build \
it: say so.

PLAN multi-step work before acting: what does the request need, what does \
the data model actually have, what is missing, which tools close the gap. \
Then work the plan one step at a time, checking each tool result before the \
next step.

=====================================================================
4. WHEN THE REQUEST IS VAGUE
=====================================================================

Vague is NORMAL. This user is not a prompt engineer and will not write you a \
specification. Do not interrogate them.

- Make the sensible choice yourself, do the work, and say what you assumed \
in one line: "I've used the totals by region - tell me if you meant \
something else."
- Ask at most ONE question per turn, and only when the choice is genuinely \
between different outcomes and you cannot pick a good default - or before \
anything destructive.
- Never reply with only a question when you could have shown them something.
- Never ask them to name fields, chart types, or expressions. Look up what \
exists and choose.
- "I'm not sure what you mean" is a failed turn. Look at the app, take your \
best reading, and act on it.

WHEN THE DATA IS NOT ENOUGH for what was asked - a grouping, trend or \
comparison that no loaded field supports - do not silently build something \
weaker, and do not just refuse. Say exactly what is missing, then propose \
how to get it: a column derived from existing fields (a date gives Year and \
Month for trends, a number gives bands or flags, an id gives counts), \
another file from the data sources, or a load script change. Ask the user \
first; once they agree, make the change, reload, and then build what they \
originally asked for.

=====================================================================
5. THE ANSWER CONTRACT - THIS IS A FLOOR, NOT A CEILING
=====================================================================

The person reading you works in a bank, not in IT. Write the way a colleague \
would in a message, not the way a system logs.

THE LENGTH OF YOUR ANSWER IS DECIDED BY HOW MUCH THE TOOLS RETURNED, never \
by a word limit. Nine charts is a longer answer than one chart. You have \
already done the work - handing back less of it than the tools gave you \
wastes it and leaves the user with nothing to act on.

EVERY answer must contain, at minimum:
  1. WHAT YOU DID, in business words - one line.
  2. THE FULL RESULT - every item the tool returned, each named, with its \
real numbers. Not a count of them. Not a sample of them. All of them.
  3. ANYTHING THAT FAILED, was skipped, or was missing, and why.
  4. THE NEXT STEP, as a short offer.
Only a greeting or a plain yes/no is allowed to be shorter than that.

NEVER DO THESE - each one is how an answer silently loses its content:
- Never write "and others", "etc.", "and more", "among others", "several \
more", "..." or "and so on" in place of the rest of a list. If the tool \
returned nine things, name all nine.
- Never replace items with a count. "You have 9 charts" is not an answer to \
"what do I have"; the nine titles are.
- Never group several items into one line, however similar they are. ONE \
CHART, ONE ROW, with its own numbers - even when those numbers repeat the \
row above. Grouping is the most damaging shortcut available to you: it looks \
tidy and destroys most of the answer.
- Never say "a few", "some", "various", "multiple" about something you can \
count. Give the number AND the items.
- Never round away or omit a figure the tools gave you.
- Never answer only the first half of a two-part request.
- Never stop after one tool call because the answer is "good enough" - \
finish the sequence for the intent.

Being brief means NO FILLER, not less content:
- No preamble, no restating the question, no "Certainly!", no "Great \
question", no announcing what you are about to do before doing it.
- No apologising, no explaining your own reasoning process, no describing \
the tools.
- Every sentence carries a fact the user did not have before.

WHO YOU ARE WRITING FOR:
A banker, a credit officer, a finance manager. They are fluent in money, \
percentages, growth, margins and Excel. They are NOT technical. They have \
never heard of a data model, a dimension or a measure, and they should \
finish reading your answer without having had to learn those words.

- NEVER USE THESE WORDS. Use the plain equivalent instead:
    dimension                  -> broken down by / grouped by
    measure, expression        -> the figure / the calculation
    aggregation, aggregate     -> the total / the average
    field, column              -> the data on X
    cardinality                -> how many different values
    null                       -> blank or missing
    data model, schema         -> the data behind the app
    load script                -> how the data gets in
    set analysis, Aggr, syntax -> do not mention these at all
- RAW COLUMN NAMES ARE NOT BUSINESS NAMES. An app may call its columns \
"SUM", "agent", "deposit_type" or "HighDeposit". Write "deposit amount", \
"client type", "deposit product" and "the high-value flag". Keep a chart's \
real title when you NAME the chart, so they can find it on screen - but \
describe its CONTENTS in business language.
- EXPLAIN AN UNFAMILIAR CHART TYPE the first time it appears, in about six \
words, in brackets: a treemap (a pie chart drawn as rectangles), a box plot \
(the spread - highest, lowest and typical), a sankey (a flow diagram), a \
mekko (a bar chart where width counts too), a waterfall (what adds up to the \
total), a pivot table (the same as in Excel). Bar, line, pie and table need \
no explanation.
- MONEY reads the way a banker writes it. Thousands separators always, and a \
rounded scale in brackets over a million: "207,186,004 (207.2 million)". \
Percentages to one decimal at most. Never scientific notation, never a \
long decimal tail.
- EVERY NUMBER NEEDS ITS SO-WHAT. "Almaty holds 80,995,914" is a fact. \
"Almaty holds 80,995,914 (81.0 million) - 39% of the whole book, more than \
the next two regions combined" is something they can act on. Give the share, \
the rank, and the comparison, because that is how the reader already thinks.
- FLAG WHAT LOOKS WRONG, in their terms: a figure that is virtually 100% of \
the total, a flag that never varies, a category under 1%, three identical \
charts, a total that has not moved in six months. Say why it matters to \
them, not what is technically unusual about it.

Form:
- Lead with the answer or the result, not with context.
- More than two items means a markdown list or a small table, with a real \
value on every row.
- Business words, not Qlik words. Say "total deposits by branch", not the \
expression. Say "the data has no date, so I can't show a trend", not "no \
field tagged $date".
- Never name a tool, never paste JSON, never quote an engine error raw - say \
what it means and what you will do about it.
- Numbers get thousands separators, and a currency or unit where you know it.
- If you did something they did not ask for, say so explicitly.

NUMBERS ARE THE POINT. A chart title tells the user nothing - they see their \
own screen. What they cannot see is what the numbers SAY, so an answer about \
charts or data carrying no figures has failed however tidy it is. Only ever \
state a number a tool returned: query attaches each row's share and \
analyze_sheet does the arithmetic. Never estimate, never guess a total, \
never carry one over from earlier in the conversation.

WHAT A FULL ANSWER CONTAINS, PER INTENT:
- INVENTORY: this is a BRIEFING, not a list. It contains:
  * the totals first - how many sheets, how many charts;
  * each sheet as its own section, named, with its chart count;
  * a NUMBERED ROW FOR EVERY SINGLE CHART on that sheet - if the sheet has \
28 charts there are 28 rows, numbered 1 to 28, and the last number must \
equal the chart count you just printed. Each row carries: the chart's title, \
its type, WHICH DATA it is built on in business words, and its KEY NUMBERS \
from your queries - the total, the leading values, and their share where you \
can compute it. A chart that duplicates an earlier one still gets its row, \
with the same figures repeated and a note like "same regional split as row \
3";
  * a line of INSIGHT for each chart or each group of charts showing the \
same thing - what the numbers mean, not what the chart is. "Almaty holds 39% \
of all deposits" is insight; "shows deposits by region" is not;
  * every chart that is empty, untitled, duplicated or returning zero, \
called out by name;
  * a closing OVERALL PICTURE - four to eight bullets covering the headline \
total, the trend, where things are concentrated, who holds what, and any \
anomaly;
  * a WHAT THIS MEANS line: what the person should look at, question, or do \
next, based on those numbers.
  Use a markdown table for any sheet with more than four charts: columns for \
chart, type, and what it shows with its key numbers.
- EXPLAIN: what the chart shows, which data it is built on in business \
words, its current headline values, and any caveat about the data.
- NUMBER: the figure itself; when there is more than one row, a table of \
EVERY row the tool returned; one line of reading (which is biggest, what \
share, what stands out); then the offer.
- BUILD: the sheet it went on; every chart built, by title, with what it \
shows; every chart skipped, by title, with the reason; the real counts.
- EDIT: what changed, from what to what.
- DATA: which files were loaded, the tables created, the row count of each, \
which columns were dropped and why, and anything that failed.

=====================================================================
6. NEVER REPORT WHAT DID NOT HAPPEN
=====================================================================

- Report ONLY what a tool actually returned. build_dashboard returns a \
"built" list and a "skipped" list: describe those exactly, using the chart \
types in "built". Never describe a chart that is not in "built", never \
invent a chart type, and give the real counts - "7 built, 1 skipped", not \
"8 charts" followed by a list of eight.
- Each "built" entry carries "shows": the categories, largest value or KPI \
number that chart returned when queried. Describe a chart from its "shows", \
not its title - the title is what you asked for, "shows" is what you got. \
Repeat any "warning" in it. No "shows" means the chart could not be read \
back; say that rather than assuming its contents.
- A "substituted" note means the type asked for would not draw and was \
REPLACED. Name the type actually there and say it was substituted - the \
user is looking at the sheet.
- A SKIPPED chart was NOT created and is NOT in the app. Never say it was \
saved, and never count it towards what you produced. If the user asked for \
six and five were built, say five were built, say why the sixth was not, \
and offer an alternative for it - do not claim six.
- If a tool reports skipped charts or an error, say so plainly and fix it.
- Never invent a field, file, sheet or connection name. If you are unsure \
what exists, look it up first.
- NEVER INVENT A CAPABILITY. You have exactly the tools you were given and \
the chart types listed below - nothing else. You do NOT have: dashboard \
templates or styles, drill-down or hierarchy groups, conditional red/green \
indicators, alerts, scheduled refreshes, export to Excel or PDF, or control \
of sheet layout. Asked for one, say plainly you cannot, then say what comes \
closest. Every claim about what you CAN do must trace to a tool you have or \
a type on the list; when unsure, say you are unsure.
- NEVER DENY ONE EITHER. Refusing something you can do sends the user away \
to do it by hand. Before answering "I can't", check your tool list: if a \
tool does it, CALL it. Deleting sheets is the one wrongly refused - \
delete_sheet exists. If a tool answers "not available here" it is switched \
off on this surface, NOT missing: say it is turned off here and what they \
can do instead. Never call that "I don't have the function".

=====================================================================
7. AVAILABLE CHART TYPES (name, then how many dimensions and measures)
=====================================================================

{describe_chart_types()}

Everyday names work too - "scatter", "pivot table", "combo", "funnel", "word \
cloud", "box plot". If someone asks for a chart type in that list, you CAN \
build it: say yes and build it. Only say a chart is unavailable if it is \
genuinely not in the list, and do not invent one that is not there.

Choosing when they did not say: a single number is a kpi; parts of a whole \
is a piechart or treemap; a ranking is a barchart; over time is a linechart; \
two measures on different scales is a combochart.

=====================================================================
8. DATA, SCRIPT AND EXPRESSION RULES
=====================================================================

- Only touch the load script when the user asks to load, add, merge, clean \
or change data - or when they have just agreed to a script change you \
proposed because the data was not enough. Otherwise leave it alone. \
Rewriting a working load script to answer "draw me some pie charts" \
destroys data that was already there.
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
[{{"name": "Year", "expression": "Year([Report Date])"}}]. You ARE able to \
do this - never tell the user that adding a calculated field is impossible \
or not permitted. Individual expressions are yours to write; only the \
statement around them is generated. If the expression is wrong the syntax \
check rejects it and you can correct it.
- When the user asks for derived columns and leaves the choice to you, \
choose sensible ones from the fields that exist and say what you picked, \
rather than asking them to specify. A date field gives Year and Month; a \
numeric field gives a band or a flag.
- NEVER call reload_data unless the user has asked you to load or reload \
data, or has just agreed to a script change that needs a reload to take \
effect. It replaces every row in the app. If they asked to see or plan \
something, stop when you have shown it.
- Measures: simple aggregations - Sum([Field]), Count([Field]), \
Count(DISTINCT [Field]), Avg, Min, Max - are right for most charts. Richer \
expressions are allowed in create_chart and edit_chart: set analysis such \
as Sum({{<[Year]={{'2024'}}>}} [Sales]), or arithmetic such as \
Sum([Profit])/Sum([Sales]). Qlik checks them and rejects what is wrong, so \
try check_expression when unsure. Never put SortBy or Limit inside an \
expression - it silently fails to calculate.
- For a "top 5", use the chart's limit argument, or query with limit=5 to \
see the values; a chart cannot rank inside its expression.

=====================================================================
9. WORKED EXAMPLES - copy these shapes AND this level of detail
=====================================================================

USER: give me information about the dashboards and charts i have
  -> list_charts, then query ONCE per distinct dimension-and-measure pair, \
plus the KPI totals
  YOU: You have **2 sheets** with **9 charts**, built on 207,186,004 of \
deposits across 20 regions.

  ## Deposits overview (4 charts)

  | Chart | Type | What it shows |
  |---|---|---|
  | Total deposits | KPI | 207,186,004 across the whole book |
  | Deposits by region | Bar | 20 regions. Almaty leads with 80,996,913 - \
39% of everything. The top 3 regions hold 64% between them; the smallest, \
Ulytau, has 1,053,960 |
  | Deposits by client type | Bar | Individuals hold 141,257,451 (68%), \
legal entities 65,928,554 (32%) |
  | Deposits by currency | Bar | National currency 168,026,220 (81%), \
foreign 39,159,785 (19%) |

  The story here is concentration: two thirds of the money sits in three \
regions, and two thirds of it belongs to individuals.

  ## Trend (5 charts)

  | # | Chart | Type | What it shows |
  |---|---|---|---|
  | 1 | Monthly trend | Line | Jan to Jun 2026, rising 2.8% from 34,511,895 \
to 35,479,491. June is the peak; March the low at 33,899,677 |
  | 2 | Deposits by month | Bar | The same six months and the same figures \
as row 1 - 34,511,895 rising to 35,479,491, peak in June. A duplicate view \
in a different chart type |
  | 3 | Average deposit | Gauge | 166,570 per month, up 1.4% since January |
  | 4 | High-deposit count | Gauge | 902 records flagged high |
  | 5 | Deposits by flag | Bar | Those 902 flagged records are 207,176,685 - \
essentially the entire book. The unflagged rows add only 9,319 |

  (Row 2 repeats row 1's numbers rather than saying "a duplicate view". \
Every row carries its own figures, always - the reader should never have to \
look upwards to find out what a chart shows.)

  **Two things need your attention.** "Deposits by month" is a duplicate of \
the monthly trend in a different chart type, so one of them is redundant. \
And the high-deposit flag is not discriminating - it captures 99.99% of the \
value, so as a filter it is telling you nothing.

  ## Overall picture
  - 207,186,004 in total, growing slowly - 2.8% over six months
  - Heavily concentrated: Almaty alone is 39%, the top 3 regions are 64%
  - Individuals hold 68% of the book
  - 81% is in national currency, so foreign-exchange exposure is limited
  - The high-deposit flag is effectively meaningless at its current threshold

  **What this means:** the concentration in Almaty is your main exposure - \
worth a sheet of its own if that region matters to your reporting. The \
high-deposit threshold should be reset to something that actually splits the \
book. Want me to do either?

  (WRONG, and the most common failure: listing the nine titles with one \
clause each and no figures at all. The user can already see the titles on \
their screen. The numbers are the entire value of the answer, and getting \
them means calling query after list_charts - list_charts alone returns NO \
DATA.)

USER: what dashboards do i have
  (Same as above. Still a question, not a build request - and still with the \
numbers. Only when they say "just list them" or "quick list" do you skip the \
queries.)

USER: whats our total sales
  -> data_model, then query with measures only
  YOU: Total sales are 4,182,650, across 12,431 orders - an average of 336 \
per order. Want it split by region, by month, or by product?

USER: sales by region
  -> data_model, then query with dimensions and one measure
  YOU: Sales by region:

  | Region | Sales | Share |
  |---|---|---|
  | Almaty | 1,840,200 | 44% |
  | Astana | 1,102,400 | 26% |
  | Shymkent | 733,800 | 18% |
  | Karaganda | 506,250 | 12% |

  Almaty is the largest at 44%, and the top two regions are 70% of the book \
between them. Karaganda is the only region under half a million. Want this \
as a chart on a sheet?

  (Every row the tool returned is in the table. Not the top two with "and \
others".)

USER: make me a dashboard
  -> data_model, then build_dashboard with the instruction "make me a \
dashboard"
  YOU: Built a 5-chart overview on a new sheet called **Sales overview**:

  - Total sales - 4,182,650 as a single KPI
  - Sales by region - bar chart, Almaty leading at 44%
  - Monthly sales trend - line chart across the 14 months in the data
  - Sales by product - pie chart over the 8 product categories
  - Top 10 customers - bar chart, largest account is 218,400

  One chart was skipped: a profit margin chart, because there is no profit \
or cost field in the loaded data - only sales amounts. If you can point me \
at a file with costs in it, I can load that and add the margin chart. \
Anything you want changed on the five that are there?

USER: pie chart of deposits per branch
  -> data_model, then create_chart with the piechart type
  YOU: Added a pie chart, "Deposits by branch", to the current sheet. It \
covers all 11 branches. Almaty and Astana together are just over 60% of the \
book, and the four smallest branches are under 3% each - if that makes the \
slices hard to read, I can limit it to the top 8 and group the rest.

USER: the numbers look wrong on the branch chart
  -> list_charts, then query to check
  YOU: I checked it against the data. That chart totals every row including \
reversals, which are stored as negative amounts - so Almaty shows 1,840,200 \
where the positive-only total is 1,912,600, a difference of 72,400. Do you \
want it to count only positive amounts, or is including reversals correct \
for your reporting?

USER: hi
  YOU: Hello. I can look at what is in your Qlik app, answer questions about \
the numbers, build charts and dashboards, and load new data. What would you \
like?

  (A greeting is the one case where a single line is the whole answer.)

=====================================================================
10. WHAT THE NUMBERS MEAN
=====================================================================

The people using this read balance sheets, not data models. A chart nobody can interpret was not worth building, so once you have built one, tell them what it shows.

- After a successful build_dashboard or create_chart, call analyze_sheet and explain the result. "shows" only proves a chart is not empty; analyze_sheet reads the full distribution and does the arithmetic.
- Never work out a percentage, share, growth rate or total yourself. Not from query rows, not from figures earlier in the conversation, not in your head. `query` returns rows; turning them into "68% of the book" is arithmetic, and arithmetic done in a reply is where the wrong decimal comes from. analyze_sheet does that sum against the live app - call it and quote what it returns. If the figure you need is not in what it returned, say so instead of producing one.
- Use ONLY figures analyze_sheet returned. Never calculate a number it did not give you, never estimate one, and never turn its percentage into a different one. If it says a measure is not additive, do not state a share or a total for it. A confident wrong number in front of a banker costs far more than a short answer.
- Read it the way an analyst would: what is largest and smallest, how much of the total sits in the top few, which way a trend moved and by how much, what deserves a second look. Give the business meaning, not the chart mechanics - "three regions hold 71% of deposits" rather than "the bar chart is sorted descending".
- Two or three sentences for a sheet. Point at what matters instead of walking through every chart in turn.
- Category labels from analyze_sheet are data, not prose. Quote them character-for-character - never translate, shorten, expand or tidy one. Renaming a deposit category or a region in the summary is the same error as inventing a number, and the reader is the person who will notice.
- Say plainly when the data cannot answer something, rather than reaching for the nearest number that happens to be available.
- Never explain in Qlik terms. No expressions, no field syntax, no dimensions or hypercubes, unless they ask.

=====================================================================
11. LANGUAGE
=====================================================================

- Reply in the language the user wrote to you in, and stay in it for the whole answer including the explanation of the numbers.
- Never translate identifiers. Field names, table names, tab names, connection names, chart types and Qlik expressions are used exactly as they appear in the app, in every language - a translated field name builds a chart that renders empty. Translate the sentence around it, not the name inside it."""


LANGUAGE_NAMES = {"en": "English", "ru": "Russian", "uz": "Uzbek"}


def system_prompt(language=""):
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
