"""What the numbers in a sheet actually say.

The assistant builds charts from field metadata - names, tags, distinct
counts - and never reads a value. That is enough to build and useless to
explain: "deposits fell 12% against last year" is a claim about values, and
a model that has not seen the values will invent it. Asking a small local
model to both fetch the rows and do the arithmetic gets a fluent paragraph
with the wrong numbers in it, which for a banking user is worse than no
paragraph at all.

So the rows are read here and the arithmetic is done here. Every fact handed
to the model is one it did not work out: a total, a share, a change between
two periods. Its job is to put these into the reader's own words. That is
the rule dashboard_builder applies to field names - check it against the
live app before letting it through - applied to sentences.

Facts are plain dicts of numbers, deliberately not sentences. Phrasing them
is the model's half, and it has to happen in whatever language the user is
writing in.
"""

import difflib
import logging
import re

log = logging.getLogger(__name__)

# How many categories to pull per chart. Enough that the share and
# concentration numbers are computed over the real distribution rather than
# a truncated head of it; small enough to stay one cheap query.
ROW_LIMIT = 500

# Charts that carry no measure to talk about. A filterpane is a control, and
# a histogram bins one field without aggregating a second.
NO_MEASURE_TYPES = {"filterpane", "histogram", "qlik-word-cloud"}

_TIME_HINTS = ("year", "date", "month", "quarter", "week", "day", "period")
_TIME_TAGS = {"$date", "$timestamp"}

# Only a sum or a count adds up across categories. Averaging the averages of
# five regions is not the average, and "37% of the total" computed over a
# column of averages is a number that means nothing - the kind of mistake a
# financial reader spots immediately and stops trusting the tool over.
_ADDITIVE = re.compile(r"^(sum|count)\s*\(", re.IGNORECASE)

# Count(DISTINCT x) looks like a count but does not add up either: a client
# with deposits in two regions is counted once per region, so the regional
# counts sum past the true distinct total and every share computed against
# that sum is overstated. DISTINCT does not have to come first - a set
# analysis or TOTAL sits in front of it, as in
# Count({<[Year]={'2026'}>} DISTINCT [Client]) - and a count written that
# way adds up no straighter than the plain one.
_COUNT_DISTINCT = re.compile(r"^count\s*\(.*\bdistinct\b", re.IGNORECASE | re.DOTALL)

# Field names are quoted in brackets and are not keywords: an app with a
# field called [Distinct Clients] is counting rows like any other.
_BRACKETED = re.compile(r"\[[^\]]*\]")


def _is_additive(expression):
    """True only for a single Sum or Count spanning the whole expression.

    Matching the opening call is not enough. Sum([Profit])/Sum([Sales]) also
    starts with "Sum(" and is a margin: margins do not add up across regions,
    so a total or a share of one is a wrong number wearing a percent sign.
    The aggregation has to close at the end of the string, with nothing
    further done to it.
    """
    text = (expression or "").strip()
    if text.startswith("="):
        text = text[1:].strip()
    if not _ADDITIVE.match(text):
        return False
    if _COUNT_DISTINCT.match(_BRACKETED.sub("[]", text)):
        return False

    depth = 0
    for index, character in enumerate(text):
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return index == len(text) - 1
    return False


def _is_time_field(name, tags):
    if {t.lower() for t in tags or []} & _TIME_TAGS:
        return True
    low = (name or "").lower()
    return any(hint in low for hint in _TIME_HINTS)


def _number(value):
    """The value as a float, or None if it is text."""
    if isinstance(value, bool):
        return None
    return float(value) if isinstance(value, (int, float)) else None


def _round(value):
    """Round for transport, not for display.

    The model formats these for the reader. Rounding here only stops a
    float's full binary expansion going into the prompt as forty characters
    of noise.
    """
    if value is None:
        return None
    if abs(value) >= 100:
        return round(value, 1)
    return round(value, 4)


def _percent(part, whole):
    if not whole:
        return None
    return round(part / whole * 100, 1)


def _label(row, column):
    value = row.get(column)
    return value if isinstance(value, str) else ("" if value is None else str(value))


def _ranking_facts(pairs, additive, is_time=False):
    """Facts about how a measure is distributed across categories.

    pairs is [(label, value)] with the values already numeric.
    """
    facts = {}
    if not pairs:
        return facts

    ordered = sorted(pairs, key=lambda p: p[1], reverse=True)
    total = sum(value for _, value in ordered)

    facts["categories"] = len(ordered)
    top_label, top_value = ordered[0]
    facts["highest"] = {"label": top_label, "value": _round(top_value)}

    if len(ordered) > 1:
        bottom_label, bottom_value = ordered[-1]
        facts["lowest"] = {"label": bottom_label, "value": _round(bottom_value)}

    if not additive:
        # Shares and totals are meaningless over an average or a min/max.
        return facts

    facts["total"] = _round(total)
    if total:
        facts["highest"]["share_pct"] = _percent(top_value, total)

    # A negative category shrinks the net total, so the biggest category's
    # share of it can pass 100% - arithmetic-true, reader-false. The shares
    # stay, but never as a bare number the model can quote unqualified.
    negatives = sum(1 for _, value in ordered if value < 0)
    if negatives:
        facts["negative_categories"] = {
            "count": negatives,
            "note": (
                "shares are of the net total, which negative categories "
                "shrink - a share can exceed 100%; say so if quoting one"
            ),
        }

    # Concentration is the question a portfolio or deposit book is actually
    # read for: how much of the book sits in a handful of names. It says
    # nothing about a date axis, where "the top 3 months hold 50%" of six
    # months is arithmetic, not a finding.
    if not is_time and len(ordered) >= 4 and total:
        top3 = sum(value for _, value in ordered[:3])
        facts["concentration"] = {
            "top_n": 3,
            "share_pct": _percent(top3, total),
            "labels": [label for label, _ in ordered[:3]],
        }

    return facts


def _trend_facts(pairs):
    """Change across an ordered series.

    pairs is [(label, value)] in the order the query returned them, which for
    a time dimension is the dimension's own sort order - chronological.
    """
    if len(pairs) < 2:
        return {}

    (first_label, first_value), (last_label, last_value) = pairs[0], pairs[-1]
    peak = max(pairs, key=lambda p: p[1])
    trough = min(pairs, key=lambda p: p[1])

    change = last_value - first_value
    trend = {
        "periods": len(pairs),
        "first": {"label": first_label, "value": _round(first_value)},
        "last": {"label": last_label, "value": _round(last_value)},
        "change": _round(change),
        "direction": "up" if change > 0 else "down" if change < 0 else "flat",
        "peak": {"label": peak[0], "value": _round(peak[1])},
        "trough": {"label": trough[0], "value": _round(trough[1])},
    }
    if first_value:
        trend["change_pct"] = _percent(change, abs(first_value))

    # The step into the most recent period is what gets asked about first,
    # and it is invisible in a start-to-end change over many periods.
    if len(pairs) >= 3:
        previous_label, previous_value = pairs[-2]
        latest_change = last_value - previous_value
        trend["latest_step"] = {
            "from": previous_label,
            "to": last_label,
            "change": _round(latest_change),
            "change_pct": _percent(latest_change, abs(previous_value)) if previous_value else None,
        }

    return trend


def analyse_chart(engine, chart, tags_by_field=None):
    """Read one chart's own numbers and reduce them to facts.

    The chart is queried through its real dimension and measure, so the facts
    describe what the reader is looking at rather than something adjacent to
    it.
    """
    title = chart.get("title") or "(untitled)"
    chart_type = chart.get("type") or ""
    dimensions = chart.get("dimensions") or []
    measures = chart.get("measures") or []

    described = {"chart": title, "type": chart_type}

    if chart_type in NO_MEASURE_TYPES or not measures:
        described["skipped"] = "no measure to summarise"
        return described

    # One dimension and one measure is what a fact can be stated about. A
    # sankey or a scatter carries more; the first of each is the spine of it,
    # and saying something true about that beats saying nothing.
    measure = measures[0]
    dimension = dimensions[0] if dimensions else None
    described["measure"] = measure
    if dimension:
        described["dimension"] = dimension
    if len(measures) > 1 or len(dimensions) > 1:
        described["note"] = "summarised on its first dimension and measure only"

    additive = _is_additive(measure)
    is_time = bool(dimension) and _is_time_field(dimension, (tags_by_field or {}).get(dimension))

    try:
        # Chronological order for a time dimension, largest-first otherwise.
        # A trend read off a measure-sorted series is not a trend.
        result = engine.query(
            dimensions=[dimension] if dimension else [],
            measures=[measure],
            limit=ROW_LIMIT,
            sort_by_measure=not is_time,
        )
    except Exception as e:  # a chart that cannot be read is not a failed turn
        log.debug("Could not read %r: %s", title, e)
        described["skipped"] = f"could not be read: {e}"
        return described

    columns = result.get("columns") or []
    rows = result.get("rows") or []
    if not columns or not rows:
        described["skipped"] = "no rows"
        return described

    measure_column = columns[-1]

    if not dimension:
        # A KPI: one aggregated number, and the number is the whole fact.
        value = _number(rows[0].get(measure_column))
        if value is None:
            described["skipped"] = "value is not numeric"
        else:
            described["facts"] = {"value": _round(value)}
        return described

    dimension_column = columns[0]
    pairs = []
    for row in rows:
        value = _number(row.get(measure_column))
        if value is not None:
            pairs.append((_label(row, dimension_column), value))

    if not pairs:
        described["skipped"] = "no numeric values"
        return described

    facts = _ranking_facts(pairs, additive, is_time=is_time)
    if is_time:
        facts["trend"] = _trend_facts(pairs)
    if not additive:
        facts["measure_is_not_additive"] = (
            "shares and totals do not apply to this measure - do not state one"
        )

    total_rows = result.get("total_rows")
    if isinstance(total_rows, int) and total_rows > len(pairs):
        facts["truncated"] = {"read": len(pairs), "of": total_rows}

    described["facts"] = facts
    return described


def analyse_sheet(engine, sheet=None, chart_ids=None):
    """Facts for every chart on a sheet.

    sheet: sheet title, or a fragment of one. Omitted means the sheet with
        the charts most recently added to it - which after a build is the one
        just built.
    chart_ids: analyse exactly these instead, wherever they live.
    """
    charts = engine.list_charts()
    if not charts:
        return {"error": "There are no charts in this app yet."}

    if chart_ids:
        wanted = set(chart_ids)
        selected = [c for c in charts if c.get("id") in wanted]
        missing = sorted(wanted - {c.get("id") for c in selected})
        if not selected:
            return {"error": f"No chart matches {', '.join(missing)}."}
        # The summary names the sheet it read; None here looks like a bug to
        # the model and gets echoed to the reader. Name the charts' own
        # sheet(s) when they have any, and say so plainly when they don't.
        sheets = sorted({c["sheet"] for c in selected if c.get("sheet")})
        title = ", ".join(sheets) if sheets else "(charts not on any sheet)"
    else:
        by_sheet = {}
        for chart in charts:
            if chart.get("sheet"):
                by_sheet.setdefault(chart["sheet"], []).append(chart)
        if not by_sheet:
            return {"error": "No chart in this app is on a sheet."}

        if sheet:
            wanted = sheet.strip().lower()
            match = next(
                (name for name in by_sheet if name.lower() == wanted),
                next((name for name in by_sheet if wanted in name.lower()), None),
            )
            if match is None:
                return {
                    "error": f"No sheet called {sheet!r}.",
                    "sheets": sorted(by_sheet),
                }
            title = match
        else:
            # list_charts walks the app in creation order, so the sheet whose
            # charts appear last is the one most recently built. A trailing
            # chart with no sheet says nothing about recency, so the walk
            # continues backwards to the last chart that has one - falling
            # back to the alphabetically last name would present an arbitrary
            # sheet as the one just built.
            title = next(
                c["sheet"] for c in reversed(charts) if c.get("sheet") in by_sheet
            )

        selected = by_sheet[title]
        missing = []

    # Qlik's own tags are what distinguish a date field from a number that
    # happens to be called "Period".
    try:
        tags_by_field = {f["name"]: f.get("tags", []) for f in engine.get_fields()}
    except Exception:  # pragma: no cover - metadata is a nicety here
        tags_by_field = {}

    analysed = [analyse_chart(engine, chart, tags_by_field) for chart in selected]

    summary = {
        "sheet": title,
        "charts": analysed,
        "read": sum(1 for a in analysed if "facts" in a),
        "instruction": (
            "These numbers were read from the app just now. Say what they "
            "show, in the user's own language, and use only figures that "
            "appear here - do not calculate new ones, and do not describe a "
            "chart that is not listed."
        ),
    }
    if missing:
        summary["unknown_chart_ids"] = missing
    return summary


# ----------------------------------------------------------------------
# Shares on an ad-hoc query
# ----------------------------------------------------------------------

# analyze_sheet only covers charts that exist, so a question like "what is
# the retail share?" sends the model to `query` instead - and then it divides
# one row by the column, in a reply, and the decimal is wrong. Telling it not
# to in the prompt does not move it off `query`: `query` is the right tool
# for the question.
#
# So the arithmetic goes where the rows come out. Whichever path the model
# takes, the percentage it quotes was computed here.

def add_shares(engine, result, dimensions=None, measures=None):
    """Attach each row's share of the total to an ad-hoc query result."""
    rows = result.get("rows") or []
    columns = result.get("columns") or []
    measures = [m for m in (measures or []) if m]
    dimensions = [d for d in (dimensions or []) if d]

    if not rows or not measures or not dimensions or not columns:
        return result

    index = len(dimensions)
    if index >= len(columns):
        return result
    column = columns[index]

    if not _is_additive(measures[0]):
        result["note"] = (
            "This measure does not add up across rows - do not state a total "
            "or a share of one for it."
        )
        return result

    values = [_number(row.get(column)) for row in rows]
    if any(value is None for value in values):
        return result

    total = sum(values)

    # A limited query returns the top few, whose sum is not the total. A
    # share against it would read as "share of the book" and be far too
    # large, so the real total is fetched rather than assumed.
    total_rows = result.get("total_rows")
    if isinstance(total_rows, int) and total_rows > len(rows):
        try:
            grand = engine.query(dimensions=[], measures=[measures[0]], limit=1)
            grand_value = _number((grand.get("rows") or [{}])[0].get(
                (grand.get("columns") or [None])[0]
            ))
        except Exception as e:
            log.debug("Could not read the grand total: %s", e)
            grand_value = None

        if grand_value is None:
            result["note"] = (
                f"These are the top {len(rows)} of {total_rows}. No share is "
                "given because the total of the rest is not known."
            )
            return result
        total = grand_value
        result["total_is_grand_total"] = True

    if not total:
        return result

    for row, value in zip(rows, values):
        row["share_pct"] = _percent(value, total)

    # Same trap as in _ranking_facts: a negative row shrinks the net total,
    # so another row's share of it can pass 100% or a share can flip sign.
    if any(value < 0 for value in values):
        result["negative_values_note"] = (
            "Some rows are negative, so shares are of the net total and can "
            "exceed 100% or be negative - qualify any share you quote."
        )

    result["total"] = _round(total)
    return result

# ----------------------------------------------------------------------
# Keeping the labels the app's own
# ----------------------------------------------------------------------

# A model writing Uzbek prose about Russian category names paraphrases them:
# "срочные и условные" comes back as "срочные и условно-срок". No prompt rule
# reliably stops a small local model doing this, and to the reader a renamed
# deposit category is the same defect as a wrong number - they cannot tell
# which of the two they are looking at.
#
# The exact strings are known, so this is repairable rather than preventable.
# Only quoted or emphasised spans are touched, which is where a model puts a
# category name, and only when the span is already nearly the label - so
# ordinary prose is never rewritten.

_QUOTED = re.compile(
    r'\*\*(?P<bold>[^*\n]{2,80})\*\*'
    r'|"(?P<double>[^"\n]{2,80})"'
    r'|«(?P<guillemet>[^»\n]{2,80})»'
    r'|“(?P<curly>[^”\n]{2,80})”'
)

# High enough that only a mangled version of the same label matches, not a
# genuinely different category that happens to share a word.
_LABEL_SIMILARITY = 0.72


def labels_in(analysis):
    """Every category label a sheet analysis reported, exactly as Qlik has it."""
    labels = set()

    def collect(node):
        if isinstance(node, dict):
            label = node.get("label")
            if isinstance(label, str) and label.strip():
                labels.add(label)
            for value in node.values():
                collect(value)
        elif isinstance(node, list):
            for item in node:
                collect(item)

    collect(analysis)
    # "labels" on a concentration fact is a plain list of names.
    if isinstance(analysis, dict):
        for chart in analysis.get("charts") or []:
            names = ((chart.get("facts") or {}).get("concentration") or {}).get("labels")
            labels.update(n for n in (names or []) if isinstance(n, str))
    return labels


def snap_labels(text, labels):
    """Restore the app's own spelling of any category name in the text.

    Returns the text unchanged when nothing is close enough, so a reply that
    quoted nothing - or quoted correctly - is passed through untouched.
    """
    if not text or not labels:
        return text

    known = {label for label in labels if len(label) >= 4}
    if not known:
        return text

    def repair(match):
        whole = match.group(0)
        for group in ("bold", "double", "guillemet", "curly"):
            candidate = match.group(group)
            if candidate is not None:
                break
        else:  # pragma: no cover - the pattern always fills one group
            return whole

        stripped = candidate.strip()
        if not stripped or stripped in known:
            return whole

        best = difflib.get_close_matches(stripped, known, n=1, cutoff=_LABEL_SIMILARITY)
        if not best:
            return whole

        log.debug("Label %r restored to %r", stripped, best[0])
        return whole.replace(candidate, best[0], 1)

    return _QUOTED.sub(repair, text)
