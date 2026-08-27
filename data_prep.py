import logging
import math
import re

log = logging.getLogger(__name__)


SPARSE_DENSITY = 0.5

CRITICAL_DENSITY = 0.1


def analyse_tables(tables):
    findings = []

    for table in tables:
        table_name = table.get("name", "")
        rows = table.get("rows", 0) or 0

        if rows == 0:
            findings.append({
                "severity": "high",
                "table": table_name,
                "field": None,
                "issue": "empty table",
                "detail": f"{table_name} loaded 0 rows",
                "suggestion": "Check the source path and any WHERE clause in the load script.",
            })
            continue

        for field in table.get("fields", []):
            findings.extend(_field_findings(table_name, rows, field))

    order = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: order.get(f["severity"], 3))
    return findings


def _field_findings(table_name, rows, field):
    name = field.get("name", "")
    density = field.get("density")
    distinct = field.get("distinct")
    null_count = field.get("null_count")

    findings = []

    def add(severity, issue, detail, suggestion):
        findings.append({
            "severity": severity, "table": table_name, "field": name,
            "issue": issue, "detail": detail, "suggestion": suggestion,
        })

    if density is not None:
        if density <= CRITICAL_DENSITY:
            add(
                "high", "almost entirely null",
                f"{name} is populated in {density:.1%} of {rows:,} rows",
                "Drop the field, or fix the source - it cannot support a chart.",
            )
        elif density < SPARSE_DENSITY:
            if null_count is None:
                detail = f"{name} is null in {1 - density:.1%} of {rows:,} rows"
            else:
                detail = f"{name} is null in {null_count:,} of {rows:,} rows ({1 - density:.1%})"
            add(
                "medium", "sparse",
                detail,
                "Filter the nulls, or default them in the load script "
                f"(e.g. If(Len(Trim([{name}]))=0, 'Unknown', [{name}]) as [{name}]).",
            )

    if distinct == 1:
        add(
            "medium", "constant",
            f"{name} has the same value in every row",
            "Useless as a dimension; drop it unless the load script needs it.",
        )

    return findings


def quality_report(tables):
    findings = analyse_tables(tables)
    return {
        "tables": [
            {"name": t.get("name"), "rows": t.get("rows"), "fields": len(t.get("fields", []))}
            for t in tables
        ],
        "total_rows": sum((t.get("rows") or 0) for t in tables),
        "findings": findings,
        "counts": {
            "high": sum(1 for f in findings if f["severity"] == "high"),
            "medium": sum(1 for f in findings if f["severity"] == "medium"),
        },
    }


TAB_MARKER = "///$tab "
GENERATED_TAB = "AI Generated"


def split_tabs(script):
    if TAB_MARKER not in script:
        return [("Main", script)]

    tabs = []
    parts = re.split(rf"^{re.escape(TAB_MARKER)}(.*)$", script, flags=re.MULTILINE)

    if parts[0].strip():
        tabs.append(("", parts[0]))

    for index in range(1, len(parts) - 1, 2):
        tabs.append((parts[index].strip(), parts[index + 1]))

    return tabs


def join_tabs(tabs):
    newline = "\r\n" if any("\r\n" in body for _, body in tabs) else "\n"

    def styled(text):
        plain = text.replace("\r\n", "\n").replace("\r", "\n")
        return plain.replace("\n", newline)

    return "".join(
        styled(body) if not name
        else f"{TAB_MARKER}{name}{newline}{styled(body.lstrip())}"
        for name, body in tabs
    )


def tab_names(script):
    return [name for name, _ in split_tabs(script)]


def _same_tab(a, b):
    return (a or "").strip().lower() == (b or "").strip().lower()


def get_tab(script, tab_name):
    for name, body in split_tabs(script):
        if _same_tab(name, tab_name):
            return body
    return None


def set_tab(script, body, tab_name=GENERATED_TAB):
    tabs = split_tabs(script)
    body = body.rstrip() + "\n\n"

    for index, (name, _) in enumerate(tabs):
        if _same_tab(name, tab_name):
            tabs[index] = (name, body)
            break
    else:
        tabs.append((tab_name, body))

    return join_tabs(tabs)


def append_to_tab(script, body, tab_name=GENERATED_TAB):
    existing = get_tab(script, tab_name) or ""
    combined = f"{existing.rstrip()}\n\n{body.strip()}" if existing.strip() else body
    return set_tab(script, combined, tab_name=tab_name)


def delete_tab(script, tab_name):
    tabs = [t for t in split_tabs(script) if not _same_tab(t[0], tab_name)]
    return join_tabs(tabs) if tabs else ""


replace_tab = set_tab


def qlik_quote(name):
    return f"[{str(name).replace(']', ']]')}]"


def from_format_spec(path, file_table=None):
    suffix = (path or "").lower().rsplit(".", 1)[-1]

    if suffix == "xlsx":
        table = f", table is {qlik_quote(file_table)}" if file_table else ""
        return f"(ooxml, embedded labels{table})"
    if suffix == "xls":
        table = f", table is {qlik_quote(f'{file_table}$')}" if file_table else ""
        return f"(biff, embedded labels{table})"
    if suffix == "qvd":
        return "(qvd)"
    if suffix == "parquet":
        return "(parquet)"
    if suffix == "json":
        return "(json, table is 'root')"
    if suffix == "xml":
        table = f", table is {qlik_quote(file_table)}" if file_table else ""
        return f"(XmlSimple{table})"
    if suffix == "tab":
        return "(txt, utf8, embedded labels, delimiter is '\\t', msq)"

    return "(txt, utf8, embedded labels, delimiter is ',', msq)"


def looks_numeric(values):
    seen = False
    for value in values:
        text = str(value).strip()
        if text == "":
            continue
        if "_" in text:
            return False
        try:
            number = float(text)
        except (TypeError, ValueError):
            return False
        if math.isnan(number) or math.isinf(number):
            return False
        seen = True
    return seen


def numeric_columns(columns, sample_rows):
    if not sample_rows:
        return set()
    return {
        column for column in columns
        if looks_numeric([row.get(column) for row in sample_rows])
    }


def _load_field_expression(column, trim_text=True, null_tokens=(), is_numeric=False):
    quoted = qlik_quote(column)
    expression = quoted

    if trim_text and not is_numeric:
        expression = f"Trim({expression})"

    for token in null_tokens:
        literal = str(token).replace("'", "''")
        expression = f"If({expression} = '{literal}', Null(), {expression})"

    return quoted if expression == quoted else f"{expression} as {quoted}"


def normalise_derived(derived):
    cleaned = []
    for entry in derived or ():
        if isinstance(entry, dict):
            name = (entry.get("name") or "").strip()
            expression = (entry.get("expression") or "").strip()
        else:
            name, expression = (list(entry) + ["", ""])[:2]
            name, expression = str(name).strip(), str(expression).strip()

        if not name or not expression:
            continue

        match = re.search(
            r"\s+as\s+(\[[^\[\]]+\]|[^\W\d]\w*(?:\.\w+)*)\s*$", expression, flags=re.I
        )
        if match and expression.count("'", 0, match.start()) % 2 == 0:
            expression = expression[:match.start()]
        cleaned.append((name, expression.rstrip(",")))

    return cleaned


def generate_load_script(
    sources,
    mode="separate",
    drop_fields=(),
    trim_text=True,
    null_tokens=(),
    table_name=None,
    derived=(),
):
    dropped = {str(f).strip().lower() for f in drop_fields or ()}
    null_tokens = tuple(null_tokens or ())
    derived_fields = normalise_derived(derived)
    lines = []
    notes = []

    if mode not in ("separate", "concatenate"):
        raise ValueError(f"mode must be 'separate' or 'concatenate', got {mode!r}")

    if not sources:
        raise ValueError("sources is empty - there is nothing to load")

    combined_name = table_name or sources[0].get("table") or "Data"

    for index, source in enumerate(sources):
        columns = [c for c in source.get("columns", []) if c]
        kept = [c for c in columns if str(c).strip().lower() not in dropped]

        if not kept:
            notes.append(f"{source.get('path')}: every column was dropped, skipping.")
            continue

        skipped = len(columns) - len(kept)
        if skipped:
            notes.append(f"{source.get('path')}: dropping {skipped} column(s).")

        numerics = numeric_columns(kept, source.get("sample_rows"))
        if numerics:
            notes.append(
                f"{source.get('path')}: {len(numerics)} numeric column(s) left "
                "untrimmed so they stay numeric."
            )

        target = combined_name if mode == "concatenate" else (
            source.get("table") or f"Table{index + 1}"
        )

        if mode == "concatenate" and index > 0:
            lines.append(f"CONCATENATE ({qlik_quote(target)})")
            lines.append("LOAD")
        else:
            lines.append(f"{qlik_quote(target)}:")
            lines.append("LOAD")

        field_lines = [
            "    " + _load_field_expression(
                column, trim_text, null_tokens, is_numeric=column in numerics
            )
            for column in kept
        ]
        for name, expression in derived_fields:
            field_lines.append(f"    {expression} as {qlik_quote(name)}")

        lines.append(",\n".join(field_lines))
        lines.append(f"FROM {qlik_quote(lib_path(source['connection'], source['path']))}")
        lines.append(
            from_format_spec(source.get("path"), source.get("file_table")) + ";"
        )
        lines.append("")

    if derived_fields:
        notes.append(
            "Added calculated column(s): "
            + ", ".join(f"{name} = {expression}" for name, expression in derived_fields)
        )
    if trim_text:
        notes.append("Text values are trimmed on load.")
    if null_tokens:
        notes.append(f"Treated as null: {', '.join(null_tokens)}.")
    if mode == "concatenate" and len(sources) > 1:
        notes.append(
            f"{len(sources)} sources concatenated into {combined_name!r}. They should "
            "share column names; any that differ become extra columns with nulls."
        )

    return {"script": "\n".join(lines).rstrip() + "\n", "notes": notes}


def describe_data_model(engine):
    tables = engine.get_tables()

    try:
        metadata = {f["name"]: f for f in engine.get_fields()}
    except Exception as e:
        log.debug("Could not read the field list: %s", e)
        metadata = {}

    for table in tables:
        for field in table.get("fields", []):
            meta = metadata.get(field["name"], {})
            field["tags"] = meta.get("tags", [])
            field["is_numeric"] = meta.get("is_numeric", False)
            if field.get("distinct") is None:
                field["distinct"] = meta.get("cardinality")

    report = quality_report(tables)

    return {
        "tables": tables,
        "total_rows": report["total_rows"],
        "quality_findings": report["findings"],
        "quality_counts": report["counts"],
    }


def lib_path(connection_name, relative_path):
    relative_path = (relative_path or "").lstrip("/\\")
    return f"lib://{connection_name}/{relative_path}" if relative_path else f"lib://{connection_name}"
