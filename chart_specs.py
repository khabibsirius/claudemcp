import re

from chart_defaults import CHART_DEFAULTS as _GENERATED

try:
    from chart_overrides import AVAILABLE_TYPES, CHART_OVERRIDES
except ImportError:
    AVAILABLE_TYPES = ()
    CHART_OVERRIDES = {}

CHART_DEFAULTS = {**_GENERATED, **CHART_OVERRIDES}

VERIFIED_TYPES = ("kpi", "barchart", "linechart", "piechart")

CHART_TYPES = VERIFIED_TYPES + tuple(
    sorted(t for t in CHART_DEFAULTS if t not in VERIFIED_TYPES)
)

EQUIVALENTS = {
    "sn-table": ("sn-table", "table"),
    "table": ("table", "sn-table"),
    "sn-pivot-table": ("sn-pivot-table", "pivot-table", "pivottable"),
    "pivot-table": ("pivot-table", "sn-pivot-table"),
    "pivottable": ("pivottable", "pivot-table", "sn-pivot-table"),
    "sn-grid-chart": ("sn-grid-chart", "sn-pivot-table", "pivot-table"),
}


def available(chart_type):
    if not AVAILABLE_TYPES or chart_type in AVAILABLE_TYPES:
        return True

    return not any(twin in AVAILABLE_TYPES
                   for twin in EQUIVALENTS.get(chart_type, ()))


def prefer_available(chart_type):
    if not AVAILABLE_TYPES or available(chart_type):
        return chart_type
    for candidate in EQUIVALENTS.get(chart_type, ()):
        if available(candidate) and candidate in CHART_DEFAULTS:
            return candidate
    return chart_type

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
    text = (name or "").strip().lower()
    if text in CHART_TYPES:
        return prefer_available(text)
    if text in CHART_ALIASES:
        return prefer_available(CHART_ALIASES[text])
    squashed = re.sub(r"[\s_-]+", "", text)
    if squashed in CHART_TYPES:
        return squashed
    return CHART_ALIASES.get(squashed)


def describe_chart_types():
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


CROSS_TAB_TYPES = ("sn-pivot-table",)

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
    n_dims, n_meas = len(dimensions or []), len(measures or [])

    usable = []
    for candidate in FALLBACK_TYPES.get(chart_type, ("sn-table",)):
        candidate = prefer_available(candidate)
        if candidate == chart_type or candidate in usable:
            continue
        (min_d, max_d), (min_m, max_m) = chart_requirements(candidate)
        if min_d <= n_dims <= (max_d or n_dims) and min_m <= n_meas <= (max_m or n_meas):
            usable.append(candidate)

    return usable

MINIMUM_DIMENSIONS = {chart_type: 2 for chart_type in CROSS_TAB_TYPES}


def chart_requirements(chart_type):
    spec = CHART_DEFAULTS.get(chart_type)
    if not spec:
        return (0, 1000), (0, 1000)

    low, high = spec["dimensions"]
    low = max(low, MINIMUM_DIMENSIONS.get(chart_type, 0))
    return (low, high), tuple(spec["measures"])

DIMENSIONAL_TYPES = ("barchart", "linechart", "piechart", "sn-table")

DIMENSIONLESS_TYPES = tuple(
    t for t, spec in CHART_DEFAULTS.items() if spec["dimensions"][1] == 0
)

NEBULA_TYPES = ("barchart", "linechart", "piechart")

CARTESIAN_TYPES = ("barchart", "linechart")

DEFAULT_SIZES = {
    "kpi": (6, 3),
    "barchart": (12, 4),
    "linechart": (12, 4),
    "piechart": (12, 4),
}


_WIDE_TYPES = {"sn-table", "sn-pivot-table", "sn-org-chart", "qlik-sankey-chart-ext"}
_SMALL_TYPES = {"gauge", "bulletchart", "filterpane"}


def default_size(chart_type):
    if chart_type in DEFAULT_SIZES:
        return DEFAULT_SIZES[chart_type]
    if chart_type in _WIDE_TYPES:
        return (24, 6)
    if chart_type in _SMALL_TYPES:
        return (6, 3)
    return (12, 4)


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

MULTI_COLOUR = "multi"


def resolve_colour(value):
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
    palette = {"index": -1, "color": resolved}

    if chart_type == "histogram":
        if resolved != MULTI_COLOUR:
            properties.setdefault("color", {}).setdefault(
                "bar", {})["paletteColor"] = palette
    elif chart_type == "waterfallchart":
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


def _deep_copy(value):
    if isinstance(value, dict):
        return {k: _deep_copy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_deep_copy(v) for v in value]
    return value


def hypercube_owner(properties):
    if "qHyperCubeDef" in properties:
        return properties, "qHyperCubeDef"

    for key, value in properties.items():
        if isinstance(value, dict) and "qHyperCubeDef" in value:
            return value, f"{key}.qHyperCubeDef"

    return properties, "qHyperCubeDef"


def _build_from_defaults(chart_type, object_id, title, hypercube_def,
                         subtitle="", footnote="", colour=None):
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

    columns = len(merged.get("qDimensions", [])) + len(merged.get("qMeasures", []))
    if columns:
        _fit_columns(merged, columns)
        _fit_columns(properties, columns)

        if "qColumnOrder" in merged:
            properties.setdefault("columnOrder", list(range(columns)))
            properties.setdefault("columnWidths", [-1] * columns)

    return properties


ORDER_KEYS = ("qColumnOrder", "columnOrder", "qInterColumnSortOrder")


def _fit_columns(holder, columns):
    for key in ORDER_KEYS:
        if key in holder and len(holder[key] or []) != columns:
            holder[key] = list(range(columns))

    if "columnWidths" in holder and len(holder["columnWidths"] or []) != columns:
        holder["columnWidths"] = [-1] * columns


def build_properties(chart_type, object_id, title, hypercube_def, subtitle="",
                     footnote="", colour=None):
    if chart_type not in VERIFIED_TYPES and chart_type in CHART_DEFAULTS:
        return _build_from_defaults(chart_type, object_id, title, hypercube_def,
                                    subtitle, footnote, colour)

    properties = {
        "qInfo": {"qId": object_id, "qType": chart_type},
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
            properties["dataPoint"] = dict(
                properties.get("dataPoint", {}), showLabels=True
            )
        else:
            properties["lineType"] = "line"

    elif chart_type == "piechart":
        properties.update(_deep_copy(_PIE_EXTRAS))

    return properties
