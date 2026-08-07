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
VERIFIED_TYPES = ("kpi", "barchart", "linechart", "piechart", "table")

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
    "straighttable": "sn-table",
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
    "distribution": "distributionplot",
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


def chart_requirements(chart_type):
    """(min, max) dimensions and measures a chart type accepts.

    Straight from the bundle. It is the only reason we know a scatter plot
    needs two measures - build one with a single measure and it cannot draw,
    which is what happened every time a model asked for one.
    """
    spec = CHART_DEFAULTS.get(chart_type)
    if not spec:
        return (0, 1000), (0, 1000)
    return tuple(spec["dimensions"]), tuple(spec["measures"])

# Chart types whose data is grouped by a dimension. A kpi is the odd one out:
# it is a single aggregated number with no dimension at all.
DIMENSIONAL_TYPES = ("barchart", "linechart", "piechart", "table")

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
    "table": (24, 6),
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


# sn-table renders its body by walking `columnOrder`. With no column list it
# draws the title and an empty white pane - data present, nothing shown. This
# is the same class of failure as the pie chart: a missing key the component
# treats as "nothing to draw" rather than as an error. The column-indexed
# lists here are filled in by build_properties(), which is the only place that
# knows how many columns the hypercube has.
_TABLE_EXTRAS = {
    "version": "2.5.0",
    "search": {"sorting": "auto"},
    "totals": {"show": True, "position": "noTotals", "label": "Totals"},
    "scrolling": {
        "horizontal": False,
        "keepFirstColumnInView": False,
        "keepFirstColumnInViewTouch": False,
    },
    "multiline": {"wrapTextInHeaders": True, "wrapTextInCells": False},
    "usePagination": False,
    "enableChartExploration": False,
    "chartExploration": {"menuVisibility": "auto"},
    "components": [],
}


def _deep_copy(value):
    """Copies the nested dict/list literals above so callers can mutate the
    properties they get back without corrupting the module-level templates.
    `copy.deepcopy` would work too; this is explicit about what it handles."""
    if isinstance(value, dict):
        return {k: _deep_copy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_deep_copy(v) for v in value]
    return value


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

    base_cube = properties.get("qHyperCubeDef") or {}
    merged = {**base_cube, **hypercube_def}
    properties["qHyperCubeDef"] = merged

    properties["qInfo"] = {"qId": object_id, "qType": chart_type}
    properties["visualization"] = chart_type
    properties["title"] = title
    properties.setdefault("subtitle", subtitle)
    properties.setdefault("footnote", footnote)
    properties.setdefault("showTitles", True)

    resolved = resolve_colour(colour)
    if resolved and isinstance(properties.get("color"), dict):
        properties["color"] = {**properties["color"], **colour_block(resolved)}

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

    elif chart_type == "table":
        properties.update(_deep_copy(_TABLE_EXTRAS))

        columns = len(hypercube_def.get("qDimensions", [])) + len(
            hypercube_def.get("qMeasures", [])
        )
        column_indexes = list(range(columns))

        # Both levels matter: the property-level list drives rendering, the
        # hypercube-level one drives which columns the engine returns.
        properties["columnOrder"] = column_indexes
        properties["columnWidths"] = [-1] * columns  # -1 = auto
        hypercube_def.setdefault("qColumnOrder", column_indexes)
        hypercube_def.setdefault("qInterColumnSortOrder", column_indexes)
        hypercube_def.setdefault("qMode", "S")  # straight table
        hypercube_def.setdefault("qSuppressMissing", True)

    return properties
