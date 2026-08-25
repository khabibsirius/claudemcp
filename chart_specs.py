"""Property trees for the native Qlik Sense chart types.

Qlik's `CreateChild` does not fill in defaults. Whatever you send is exactly
what the client gets, and the modern nebula.js renderers assume the full
property tree a hand-built chart would have. Two distinct failure modes come
from getting this wrong, and neither raises an error at the API level:

  * bar/line (`sn-bar-chart`, `sn-line-chart`) throw "Cannot read properties
    of undefined (reading 'show')" in the browser and render an error tile,
    because they read `dimensionAxis.show` / `measureAxis.show` unguarded.
  * pie (`sn-pie-chart`) silently renders an empty box, because it ignores
    keys it doesn't recognise rather than failing.

Everything below was copied from real, manually-built, confirmed-working
objects read back with `GetProperties` - not guessed from the docs. Treat the
values as load-bearing; if you add a chart type, build one by hand in the
Qlik client first and dump it with `python dump_object_properties.py`.

To add a chart type: add it to DEFAULT_SIZES, then extend
`build_properties()` with its block.
"""

import re

from chart_defaults import CHART_DEFAULTS

# Hand-verified types. Their property trees below were copied from real,
# confirmed-rendering objects and are used in preference to the generated
# defaults, because they are known to work in this app.
#
# "table" used to be here and is not any more. Its hand-written tree was
# copied from a Qlik version that has since moved on: the object still
# computes its rows - the engine reports a full qMatrix for one - and the
# client draws an empty pane, which is the worst way for a chart to fail.
# The bundle's own "sn-table", built from the installed client's property
# tree, renders the same data correctly, so the name is aliased onto it
# below and this list is the four that are still genuinely verified.
VERIFIED_TYPES = ("kpi", "barchart", "linechart", "piechart")

# Everything Qlik ships, from chart_defaults.py. The verified five come
# first so they stay the natural choice.
CHART_TYPES = VERIFIED_TYPES + tuple(
    sorted(t for t in CHART_DEFAULTS if t not in VERIFIED_TYPES)
)

# The engine's names are not what anyone would say out loud - a funnel chart
# is "qlik-funnel-chart-ext" and a pivot table is "sn-pivot-table". These let
# a caller ask for the obvious word.
CHART_ALIASES = {
    "scatter": "scatterplot",
    "scatterplot": "scatterplot",
    "scatter plot": "scatterplot",
    "combo": "combochart",
    "combo chart": "combochart",
    "funnel": "qlik-funnel-chart-ext",
    "funnelchart": "qlik-funnel-chart-ext",
    "sankey": "qlik-sankey-chart-ext",
    "network": "qlik-network-chart",
    "wordcloud": "qlik-word-cloud",
    "word cloud": "qlik-word-cloud",
    "pivot": "sn-pivot-table",
    "pivottable": "sn-pivot-table",
    "pivot table": "sn-pivot-table",
    # "table" is the word everyone uses, and it has to reach the object that
    # actually draws - see VERIFIED_TYPES. Aliased rather than renamed so
    # every existing caller, prompt and saved instruction keeps working.
    "table": "sn-table",
    "straighttable": "sn-table",
    "straight table": "sn-table",
    "datatable": "sn-table",
    "data table": "sn-table",
    "grid": "sn-grid-chart",
    "gridchart": "sn-grid-chart",
    "orgchart": "sn-org-chart",
    "org chart": "sn-org-chart",
    "bullet": "bulletchart",
    "waterfall": "waterfallchart",
    "mekko": "mekkochart",
    "box": "boxplot",
    "box plot": "boxplot",
    "filter": "filterpane",
    "filter pane": "filterpane",
}


def resolve_chart_type(name):
    """Map whatever was asked for onto an engine chart type, or None."""
    text = (name or "").strip().lower()
    if text in CHART_TYPES:
        return text
    if text in CHART_ALIASES:
        return CHART_ALIASES[text]
    # "bar chart" / "bar_chart" -> "barchart"
    squashed = re.sub(r"[\s_-]+", "", text)
    if squashed in CHART_TYPES:
        return squashed
    return CHART_ALIASES.get(squashed)


def describe_chart_types():
    """A one-line catalogue of every type, for prompts and tool descriptions.

    Generated rather than written out, because a hand-maintained list went
    stale the moment new types were added - and the assistant then told
    people a bullet chart was impossible because the prompt said so.
    """
    lines = []
    for chart_type in CHART_TYPES:
        dimensions, measures = chart_requirements(chart_type)

        def span(low, high, noun):
            if high == 0:
                return f"no {noun}s"
            if low == high:
                return f"{low} {noun}" + ("" if low == 1 else "s")
            if high >= 1000:
                return f"{low}+ {noun}s"
            return f"{low}-{high} {noun}s"

        lines.append(
            f"{chart_type} ({span(*dimensions, 'dimension')}, "
            f"{span(*measures, 'measure')})"
        )
    return "; ".join(lines)


# A pivot table is one dimension crossed against another. The bundle says it
# accepts one, and that is true of the object: Qlik builds it, puts the single
# dimension down the side, has nothing to lay across the top, and draws an
# empty grid. The bundle describes what the object TOLERATES; a cross-tab with
# nothing to cross is not a chart anyone can read, so the real minimum is two.
#
# Corrected here rather than at each call site because this number is the
# assistant's own source of truth - describe_chart_types() prints it straight
# into the prompt, so a wrong minimum here teaches the model the wrong rule
# and every guard downstream is arguing with the catalogue.
CROSS_TAB_TYPES = ("sn-pivot-table",)

# What to build instead when a chart is created, read back, and turns out not
# to draw. Each fallback shows the SAME data a different way rather than
# retrying the same type: the failures behind this are property-tree bugs, so
# rebuilding identically reproduces them exactly.
#
# Ordered by how close each is to the original. sn-table ends every chain
# because it accepts any number of dimensions and measures and is the one
# type confirmed rendering in the user's own app, so a fallback chain cannot
# run out of options and leave nothing.
FALLBACK_TYPES = {
    "boxplot": ("barchart", "sn-table"),
    "sn-grid-chart": ("sn-pivot-table", "sn-table"),
    "mekkochart": ("sn-pivot-table", "sn-table"),
    "treemap": ("barchart", "sn-table"),
    "qlik-network-chart": ("sn-table",),
    "sn-org-chart": ("sn-table",),
    "qlik-sankey-chart-ext": ("sn-table",),
    "qlik-funnel-chart-ext": ("barchart", "sn-table"),
    "qlik-word-cloud": ("barchart", "sn-table"),
    "scatterplot": ("combochart", "sn-table"),
    "waterfallchart": ("barchart", "sn-table"),
    "bulletchart": ("barchart", "sn-table"),
    "gauge": ("kpi",),
    "histogram": ("barchart", "sn-table"),
    "piechart": ("barchart", "sn-table"),
    "combochart": ("barchart", "sn-table"),
    "sn-pivot-table": ("sn-table",),
    "barchart": ("sn-table",),
    "linechart": ("barchart", "sn-table"),
}


def fallback_types(chart_type, dimensions, measures):
    """Types that could show this data instead, best match first.

    Only types that actually accept this many dimensions and measures are
    offered. A fallback the engine would refuse is not a fallback - it turns
    one broken chart into a rejection with a confusing reason attached.
    """
    n_dims, n_meas = len(dimensions or []), len(measures or [])

    usable = []
    for candidate in FALLBACK_TYPES.get(chart_type, ("sn-table",)):
        (min_d, max_d), (min_m, max_m) = chart_requirements(candidate)
        if min_d <= n_dims <= (max_d or n_dims) and min_m <= n_meas <= (max_m or n_meas):
            usable.append(candidate)

    return usable

MINIMUM_DIMENSIONS = {chart_type: 2 for chart_type in CROSS_TAB_TYPES}


def chart_requirements(chart_type):
    """(min, max) dimensions and measures a chart type accepts.

    Straight from the bundle, except where the bundle's minimum describes
    what the object tolerates rather than what renders something readable -
    see MINIMUM_DIMENSIONS. It is the only reason we know a scatter plot
    needs two measures - build one with a single measure and it cannot draw,
    which is what happened every time a model asked for one.
    """
    spec = CHART_DEFAULTS.get(chart_type)
    if not spec:
        return (0, 1000), (0, 1000)

    low, high = spec["dimensions"]
    low = max(low, MINIMUM_DIMENSIONS.get(chart_type, 0))
    return (low, high), tuple(spec["measures"])

# Chart types whose data is grouped by a dimension. A kpi is the odd one out:
# it is a single aggregated number with no dimension at all.
DIMENSIONAL_TYPES = ("barchart", "linechart", "piechart", "sn-table")

# Types that take no dimension at all, so asking for one is an error
# rather than an omission.
DIMENSIONLESS_TYPES = tuple(
    t for t, spec in CHART_DEFAULTS.items() if spec["dimensions"][1] == 0
)

# Rendered by nebula.js components that need the full property tree below.
NEBULA_TYPES = ("barchart", "linechart", "piechart")

# Axis-based (cartesian) charts. Both failed identically without the axis
# config objects, so they share a block.
CARTESIAN_TYPES = ("barchart", "linechart")

# Default footprint on the sheet's 24-column grid, as (colspan, rowspan).
DEFAULT_SIZES = {
    "kpi": (6, 3),
    "barchart": (12, 4),
    "linechart": (12, 4),
    "piechart": (12, 4),
    # A table's footprint comes from _WIDE_TYPES below, which already lists
    # sn-table at full width.
}


# Full-width types: tables and anything that reads as a list.
_WIDE_TYPES = {"sn-table", "sn-pivot-table", "sn-org-chart", "qlik-sankey-chart-ext"}
_SMALL_TYPES = {"gauge", "bulletchart", "filterpane"}


def default_size(chart_type):
    """Grid footprint for a chart type, falling back to a half-width block."""
    if chart_type in DEFAULT_SIZES:
        return DEFAULT_SIZES[chart_type]
    if chart_type in _WIDE_TYPES:
        return (24, 6)
    if chart_type in _SMALL_TYPES:
        return (6, 3)
    return (12, 4)


# Colour names someone would actually say, mapped to readable chart colours
# rather than pure web primaries - #ff0000 on a white sheet is painful.
COLOURS = {
    "red": "#DC423F",
    "blue": "#3B76AF",
    "green": "#5AA553",
    "orange": "#E8853A",
    "purple": "#8A6BAF",
    "teal": "#1F9CA3",
    "yellow": "#E8C13A",
    "pink": "#D9709C",
    "brown": "#8C6D5C",
    "grey": "#7B7B7B",
    "gray": "#7B7B7B",
    "black": "#404040",
}

# Ask for this instead of a colour name to give every category its own
# colour, which is what "colourful" usually means.
MULTI_COLOUR = "multi"


def resolve_colour(value):
    """A colour name or #hex to a hex string, or None if unrecognised.

    Returns MULTI_COLOUR unchanged so callers can tell "one specific colour"
    from "a different colour per category".
    """
    if not value:
        return None

    text = str(value).strip().lower()
    if text in (MULTI_COLOUR, "multicolour", "multicolor", "colourful", "colorful", "rainbow"):
        return MULTI_COLOUR
    if text in COLOURS:
        return COLOURS[text]
    if re.fullmatch(r"#[0-9a-f]{6}", text):
        return text.upper()
    return None


def colour_block(colour):
    """The `color` property for an explicit choice.

    `auto` must be false or Qlik picks its own colouring and quietly ignores
    everything else here - which reads as "the colour setting does nothing".
    """
    if colour == MULTI_COLOUR:
        return {
            "auto": False,
            "mode": "byDimension",
            "useDimColVal": True,
            "persistent": True,
            "dimensionScheme": "12",
            "reverseScheme": False,
        }

    return {
        "auto": False,
        "mode": "primary",
        "paletteColor": {"index": -1, "color": colour},
        "useBaseColors": "off",
        "persistent": True,
    }


def _apply_generated_colour(chart_type, properties, resolved):
    """Wire an explicit colour into a generated property tree.

    Most bundles read the flat `color` block that colour_block() fills in,
    but a few read nested paths instead: histogram colours its bars from
    color.bar, waterfall reads color.positiveValue / color.negativeValue,
    and boxplot keeps its whole colour block inside boxplotDef. Merging the
    flat block into those left every nested default in place, so "make the
    histogram red" changed nothing and reported success.
    """

    palette = {"index": -1, "color": resolved}

    if chart_type == "histogram":
        # One series of bars, no dimension: a single colour is expressible
        # here, "multi" is not, so multi keeps the default.
        if resolved != MULTI_COLOUR:
            properties.setdefault("color", {}).setdefault(
                "bar", {})["paletteColor"] = palette
    elif chart_type == "waterfallchart":
        # Only the rises take the requested colour: the red falls are what
        # make a waterfall readable, and painting both the same hides which
        # bars are falls. No dimension either, so "multi" means nothing.
        if resolved != MULTI_COLOUR:
            colour = properties.setdefault("color", {})
            colour["auto"] = False
            colour.setdefault("positiveValue", {})["paletteColor"] = palette
    elif chart_type == "boxplot":
        if resolved != MULTI_COLOUR:
            colour = properties.setdefault(
                "boxplotDef", {}).setdefault("color", {})
            colour["auto"] = False
            colour.setdefault("box", {})["paletteColor"] = palette
    elif isinstance(properties.get("color"), dict):
        properties["color"] = {**properties["color"], **colour_block(resolved)}


# Shared by every nebula chart. Extracted from a working bar chart.
_COMMON_CHART_EXTRAS = {
    "legend": {"show": True, "dock": "auto", "showTitle": True},
    "color": {
        "auto": True,
        "mode": "primary",
        "formatting": {"numFormatFromTemplate": True},
        "useBaseColors": "off",
        "paletteColor": {"index": 6},
        "useDimColVal": True,
        "useMeasureGradient": True,
        "persistent": False,
        "expressionIsColor": True,
        "expressionLabel": "",
        "measureScheme": "sg",
        "reverseScheme": False,
        "dimensionScheme": "12",
        "autoMinMax": True,
        "measureMin": 0,
        "measureMax": 10,
    },
    "dataPoint": {
        "showLabels": False,
        "showSegmentLabels": False,
        "showTotalLabels": True,
    },
    "tooltip": {
        "auto": True,
        "hideBasic": False,
        "chart": {"style": {"size": "medium"}},
        "data": {},
    },
    "version": "2.2.0",
    "components": [],
}

_CARTESIAN_EXTRAS = {
    "script": "",
    "filter": None,
    "refLine": {"refLines": [], "dimRefLines": []},
    "plugins": [],
    "scrollbar": "miniChart",
    "scrollStartPos": 0,
    "gridLine": {"auto": True, "spacing": 2},
    "dimensionAxis": {
        "continuousAuto": True,
        "show": "all",
        "label": "auto",
        "dock": "near",
        "axisDisplayMode": "auto",
        "maxVisibleItems": 10,
    },
    "preferContinuousAxis": True,
    "measureAxis": {
        "show": "all",
        "dock": "near",
        "spacing": 1,
        "autoMinMax": True,
        "minMax": "min",
        "min": 0,
        "max": 10,
    },
    "showMiniChartForContinuousAxis": True,
}

# Reference: a manually-built pie chart (qId PsLnyCf) confirmed rendering in
# Qlik Sense Desktop. Pie does NOT share bar/line's "dataPoint" shape
# (showLabels/showSegmentLabels/showTotalLabels), and the donut toggle is
# "donut", not "slice" - both were earlier guesses that sn-pie-chart silently
# ignored, which is why it drew an empty box instead of failing loudly the
# way bar and line did. It also needs a "components" entry to style slices.
_PIE_EXTRAS = {
    "script": "",
    "filter": None,
    "dimensionTitle": True,
    "donut": {"showAsDonut": False},
    "dataPoint": {
        "auto": True,
        "labelMode": "share",
        "labelValueMode": "arc",
    },
    "components": [
        {
            "key": "slices",
            "style": {
                "strokeWidth": "none",
                "strokeColor": {"index": -1, "color": "#FFFFFF"},
                "cornerRadius": 0,
                "innerRadius": 0.55,
            },
        }
    ],
}


# A hand-written table property tree used to live here, pinned at version
# "2.5.0" while the installed bundle had moved to 6.19.0. It built an object
# that computed a full qMatrix and drew an empty pane - the failure it was
# itself written to prevent. It is gone rather than repaired: the bundle
# ships the current tree for this client, and a tree maintained by hand goes
# stale again on the next Qlik upgrade with no test able to notice.


def _deep_copy(value):
    """Copies the nested dict/list literals above so callers can mutate the
    properties they get back without corrupting the module-level templates.
    `copy.deepcopy` would work too; this is explicit about what it handles."""
    if isinstance(value, dict):
        return {k: _deep_copy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_deep_copy(v) for v in value]
    return value


def hypercube_owner(properties):
    """The dict that holds this chart's qHyperCubeDef, and its path.

    Almost every bundle keeps the hypercube at the top of the property tree,
    and a box plot does not: it keeps it at boxplotDef.qHyperCubeDef, and
    reads nothing from the top level. Writing the dimensions to the top of a
    box plot creates a cube the engine happily computes and the component
    never looks at, so the object renders Qlik's own "Incomplete
    visualization" while every check we run reports data.

    So the bundle is asked where its cube goes rather than assumed. Returns
    (owner_dict, path) with path for diagnostics; falls back to the top level
    for a bundle that ships no cube at all.
    """
    if "qHyperCubeDef" in properties:
        return properties, "qHyperCubeDef"

    for key, value in properties.items():
        if isinstance(value, dict) and "qHyperCubeDef" in value:
            return value, f"{key}.qHyperCubeDef"

    return properties, "qHyperCubeDef"


def _build_from_defaults(chart_type, object_id, title, hypercube_def,
                         subtitle="", footnote="", colour=None):
    """Assemble properties for a type we did not hand-verify.

    The base is the property tree Qlik's own client writes for this chart, so
    every key its renderer expects is present. Our dimensions and measures
    are merged *into* the bundle's own qHyperCubeDef rather than replacing
    it, because that carries per-chart settings - a scatter plot fetches a
    different data window from a bar chart, and dropping those is how a chart
    ends up drawing nothing.
    """

    properties = _deep_copy(CHART_DEFAULTS[chart_type]["properties"])

    owner, _path = hypercube_owner(properties)
    base_cube = owner.get("qHyperCubeDef") or {}
    merged = {**base_cube, **hypercube_def}
    owner["qHyperCubeDef"] = merged

    properties["qInfo"] = {"qId": object_id, "qType": chart_type}
    properties["visualization"] = chart_type
    properties["title"] = title
    properties.setdefault("subtitle", subtitle)
    properties.setdefault("footnote", footnote)
    properties.setdefault("showTitles", True)

    resolved = resolve_colour(colour)
    if resolved:
        _apply_generated_colour(chart_type, properties, resolved)

    # Tables render their body from a column list; without it they draw an
    # empty pane. The generated defaults ship an empty one.
    columns = len(merged.get("qDimensions", [])) + len(merged.get("qMeasures", []))
    if columns and "qColumnOrder" in merged and not merged["qColumnOrder"]:
        merged["qColumnOrder"] = list(range(columns))
        properties.setdefault("columnOrder", list(range(columns)))
        properties.setdefault("columnWidths", [-1] * columns)

    return properties


def build_properties(chart_type, object_id, title, hypercube_def, subtitle="",
                     footnote="", colour=None):
    """Assemble the full `qProp` payload for a CreateChild call.

    chart_type must be one of CHART_TYPES; the caller is expected to have
    validated it already.
    """

    if chart_type not in VERIFIED_TYPES and chart_type in CHART_DEFAULTS:
        return _build_from_defaults(chart_type, object_id, title, hypercube_def,
                                    subtitle, footnote, colour)

    properties = {
        "qInfo": {"qId": object_id, "qType": chart_type},
        # `visualization` is what the client itself writes alongside qType.
        # Keeping them equal matches every hand-built object we dumped.
        "visualization": chart_type,
        "title": title,
        "subtitle": subtitle,
        "footnote": footnote,
        "disableNavMenu": False,
        "showTitles": True,
        "showDetails": True,
        "showDetailsExpression": False,
        "showDisclaimer": True,
        "qHyperCubeDef": hypercube_def,
    }

    if chart_type in NEBULA_TYPES:
        properties.update(_deep_copy(_COMMON_CHART_EXTRAS))

        resolved = resolve_colour(colour)
        if resolved:
            # Merged over the defaults rather than replacing them, so the
            # keys the renderer expects to exist all survive.
            properties["color"] = {
                **properties["color"], **colour_block(resolved)
            }

    if chart_type in CARTESIAN_TYPES:
        properties.update(_deep_copy(_CARTESIAN_EXTRAS))
        if chart_type == "barchart":
            properties.update({
                "orientation": "vertical",
                "barGrouping": {"grouping": "grouped"},
            })
            # Values printed on the bars. Off by default in Qlik, but a
            # dashboard is read at a glance, and a bar you have to measure
            # against an axis by eye isn't telling you the number.
            properties["dataPoint"] = dict(
                properties.get("dataPoint", {}), showLabels=True
            )
        else:
            properties["lineType"] = "line"

    elif chart_type == "piechart":
        properties.update(_deep_copy(_PIE_EXTRAS))

    return properties
