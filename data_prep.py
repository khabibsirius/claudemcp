"""Data quality analysis and load-script generation.

Two jobs:

  * Turn `GetTablesAndKeys` statistics into findings a person (or a model)
    can act on - which columns are empty, constant, or duplicated.
  * Help write the load script that fixes them.

The engine reports raw numbers; the judgement about what counts as a problem
lives here so it is in one place and testable without a live Qlik.
"""

import logging
import math
import re

log = logging.getLogger(__name__)


# A column populated in under this fraction of rows is mostly holes. Charting
# it produces a mostly-empty visual, so it is worth flagging before anyone
# builds on it.
SPARSE_DENSITY = 0.5

# Below this it is essentially unusable.
CRITICAL_DENSITY = 0.1


def analyse_tables(tables):
    """Turn table/field statistics into a list of data quality findings.

    Each finding is {severity, table, field, issue, detail, suggestion}.
    Severity is "high" for things that make a field unusable and "medium"
    for things worth knowing before charting.
    """

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
            # The engine sometimes supplies density without a null count.
            # Defaulting the count to 0 next to a density-derived percentage
            # produced "null in 0 of 1,000 rows (60.0%)", which contradicts
            # itself - so the count is only quoted when it was reported.
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

    # One distinct value carries no information - it cannot group anything.
    if distinct == 1:
        add(
            "medium", "constant",
            f"{name} has the same value in every row",
            "Useless as a dimension; drop it unless the load script needs it.",
        )

    return findings


def quality_report(tables):
    """A compact summary plus findings, suitable for returning to a model."""
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


# ----------------------------------------------------------------------
# Load script helpers
# ----------------------------------------------------------------------

# Qlik tab markers. Keeping generated code in its own tab means a regenerated
# script never eats hand-written sections.
TAB_MARKER = "///$tab "
GENERATED_TAB = "AI Generated"


def split_tabs(script):
    """Split a load script into (tab_name, body) pairs.

    A script with no marker is treated as one unnamed tab, which is what the
    engine does.
    """
    if TAB_MARKER not in script:
        return [("Main", script)]

    tabs = []
    parts = re.split(rf"^{re.escape(TAB_MARKER)}(.*)$", script, flags=re.MULTILINE)

    # re.split leaves any text before the first marker in parts[0]. It is
    # kept under an empty name rather than relabelled "Main": naming it
    # would make join_tabs insert a marker the user never wrote, and would
    # collide with a real tab called Main further down the script.
    if parts[0].strip():
        tabs.append(("", parts[0]))

    for index in range(1, len(parts) - 1, 2):
        tabs.append((parts[index].strip(), parts[index + 1]))

    return tabs


def join_tabs(tabs):
    """Rebuild a script from (tab_name, body) pairs.

    Written in the script's own style rather than a fixed one. The engine
    hands scripts back with CRLF and generate_load_script builds LF, so a
    marker - or a freshly written tab - in the other style left the file
    carrying both, and the next diff of it showed lines nobody had edited.
    Matching what is already there rewrites nothing that was not touched.
    An empty name is the unnamed chunk split_tabs found before the first
    marker; it goes back with no marker invented for it.
    """
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
    """Just the tab names, in order - the sections of the load editor."""
    return [name for name, _ in split_tabs(script)]


def _same_tab(a, b):
    return (a or "").strip().lower() == (b or "").strip().lower()


def get_tab(script, tab_name):
    """The body of one tab, or None if there is no such tab."""
    for name, body in split_tabs(script):
        if _same_tab(name, tab_name):
            return body
    return None


def set_tab(script, body, tab_name=GENERATED_TAB):
    """Put `body` into its own tab, replacing that tab if it already exists.

    Editing one tab rather than the whole script is what makes this safe to
    do repeatedly: the model rewrites its own section over and over without
    ever touching what someone wrote by hand in another tab.
    """
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
    """Add to the end of a tab, creating it if needed."""
    existing = get_tab(script, tab_name) or ""
    combined = f"{existing.rstrip()}\n\n{body.strip()}" if existing.strip() else body
    return set_tab(script, combined, tab_name=tab_name)


def delete_tab(script, tab_name):
    """Remove a tab. Returns the script unchanged if it wasn't there."""
    tabs = [t for t in split_tabs(script) if not _same_tab(t[0], tab_name)]
    return join_tabs(tabs) if tabs else ""


# Kept for callers written against the earlier name.
replace_tab = set_tab


def qlik_quote(name):
    """Bracket-quote a field or table name for use in a script."""
    return f"[{str(name).replace(']', ']]')}]"


def from_format_spec(path, file_table=None):
    """The format spec a FROM clause needs, chosen by file extension.

    Every generated statement used to end `(txt, ...)` whatever the file
    was, so a script "loading" an .xlsx or .qvd could never run. file_table
    is the table name INSIDE the file - an Excel sheet name - not the name
    the load gives the result.
    """
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
    """True if every non-blank sample value parses as a number.

    Used to decide what NOT to clean. Trim() returns text, so trimming a
    numeric column silently turns it into strings: the field loses its
    $numeric tag and Sum() over it stops working. Guessing wrong in this
    direction breaks every measure built on the field, so a column is only
    treated as numeric when the whole sample agrees.
    """
    seen = False
    for value in values:
        text = str(value).strip()
        if text == "":
            continue
        # float() is more permissive than Qlik: it accepts "nan", "inf" and
        # underscore separators like "1_000", all of which Qlik reads as
        # text. Calling such a column numeric skips the Trim() it needs.
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
    """The subset of `columns` whose sample values are all numeric."""
    if not sample_rows:
        return set()
    return {
        column for column in columns
        if looks_numeric([row.get(column) for row in sample_rows])
    }


def _load_field_expression(column, trim_text=True, null_tokens=(), is_numeric=False):
    """One line of a LOAD field list, with cleaning applied.

    Cleaning is expressed as a Qlik expression rather than done after the
    fact, because the load script is the only place a fix applies to every
    row once and stays applied on the next reload.
    """
    quoted = qlik_quote(column)
    expression = quoted

    # Numbers are left alone - see looks_numeric.
    if trim_text and not is_numeric:
        expression = f"Trim({expression})"

    for token in null_tokens:
        # Turn placeholder strings into real nulls so Qlik counts them as
        # missing instead of charting "N/A" as a category. Both branches
        # return the original value or null, so a numeric column stays
        # numeric. Apostrophes are doubled the Qlik way - a raw "n'a" would
        # end the literal early and break the whole script.
        literal = str(token).replace("'", "''")
        expression = f"If({expression} = '{literal}', Null(), {expression})"

    return quoted if expression == quoted else f"{expression} as {quoted}"


def normalise_derived(derived):
    """Clean a list of calculated columns into [(name, expression), ...].

    Entries without both a name and an expression are dropped rather than
    producing `None as [None]`, which would fail the syntax check and take
    the whole script with it.
    """
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

        # The model sometimes includes the "as X" part; the generator adds
        # it. Only a plain identifier or [bracketed] tail counts, and only
        # outside string literals - a looser match once ate the end of
        # If(a, 'x as y'), leaving If(a, 'x and a broken script.
        # \w rather than an A-Z range: the alias is as often Год as Year,
        # and an unstripped one became "... as Год as [Год]", which the
        # syntax check rejects and the whole script with it.
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
    """Build a LOAD script for one or more previewed files.

    sources: [{connection, path, table, columns}] - as returned by
             engine.preview_file, one entry per table to load.
    mode:    "separate" keeps a table per source; "concatenate" stacks them
             into one table, which is what "merge these files" means when
             they share a shape.
    drop_fields: column names to leave out entirely - the way a second pass
             removes the constant or empty columns that only become visible
             once the data is loaded.
    derived: [{name, expression}] - columns calculated from the file's own,
             e.g. {"name": "Year", "expression": "Year([Report Date])"}. This
             is how a new field gets added at all: without it the only
             possible script is a straight copy of the file's columns, and
             "add a Year column" has no answer.

    Returns {"script": str, "notes": [str]}.
    """

    dropped = {str(f).strip().lower() for f in drop_fields or ()}
    null_tokens = tuple(null_tokens or ())
    derived_fields = normalise_derived(derived)
    lines = []
    notes = []

    if mode not in ("separate", "concatenate"):
        raise ValueError(f"mode must be 'separate' or 'concatenate', got {mode!r}")

    # An empty script written into a tab looks like success while loading
    # nothing, so no sources is an error rather than a silent no-op.
    if not sources:
        raise ValueError("sources is empty - there is nothing to load")

    # A first source with no table name used to leave the concatenate target
    # literally called None.
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

        # CONCATENATE must name the target table explicitly; a bare
        # CONCATENATE would auto-attach to whatever loaded last, which is
        # fragile once the script has more than one statement.
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
        # Calculated columns, appended after the file's own fields so an
        # expression can refer to them.
        for name, expression in derived_fields:
            field_lines.append(f"    {expression} as {qlik_quote(name)}")

        lines.append(",\n".join(field_lines))
        # qlik_quote, not bare brackets: a ] in the connection or path would
        # otherwise close the FROM clause mid-path.
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
    """One picture of the loaded data: tables, fields, and what's wrong.

    Merges the two things the engine reports separately - table statistics
    (row counts, nulls) and field metadata (tags, distinct counts) - because
    choosing a dimension needs both, and making a caller stitch them together
    is how you end up charting a 65,000-value identifier.
    """

    tables = engine.get_tables()

    try:
        metadata = {f["name"]: f for f in engine.get_fields()}
    except Exception as e:  # field list is a bonus, not a requirement
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
    """The lib:// reference a load statement uses for a connection file."""
    relative_path = (relative_path or "").lstrip("/\\")
    return f"lib://{connection_name}/{relative_path}" if relative_path else f"lib://{connection_name}"
