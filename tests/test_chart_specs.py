"""The chart property trees.

These values were read back off real, hand-built, confirmed-working objects.
The tests below don't re-derive them - they pin the keys whose absence caused
a specific, silent failure, so a future edit can't quietly drop one.
"""

import pytest

from chart_specs import (
    CARTESIAN_TYPES,
    CHART_TYPES,
    DEFAULT_SIZES,
    NEBULA_TYPES,
    build_properties,
    default_size,
)

HYPERCUBE = {"qDimensions": [], "qMeasures": [], "qInitialDataFetch": []}


def props(chart_type):
    return build_properties(chart_type, "OBJ_1", "A title", dict(HYPERCUBE))


class TestCommonShape:

    @pytest.mark.parametrize("chart_type", CHART_TYPES)
    def test_identifies_the_object(self, chart_type):
        properties = props(chart_type)
        assert properties["qInfo"] == {"qId": "OBJ_1", "qType": chart_type}
        assert properties["title"] == "A title"

    @pytest.mark.parametrize("chart_type", CHART_TYPES)
    def test_visualization_matches_qtype(self, chart_type):
        """Every hand-built object we dumped carries both, and they agree."""
        properties = props(chart_type)
        assert properties["visualization"] == properties["qInfo"]["qType"]

    @pytest.mark.parametrize("chart_type", CHART_TYPES)
    def test_carries_the_hypercube(self, chart_type):
        assert "qHyperCubeDef" in props(chart_type)

    @pytest.mark.parametrize("chart_type", CHART_TYPES)
    def test_has_a_default_size(self, chart_type):
        colspan, rowspan = default_size(chart_type)
        assert 1 <= colspan <= 24 and rowspan >= 1
        # Types beyond the original five get their size from the
        # fallbacks in default_size(), not the literal table.


class TestNebulaRequirements:
    """sn-bar-chart and sn-line-chart read these unguarded and throw
    "Cannot read properties of undefined (reading 'show')" without them."""

    @pytest.mark.parametrize("chart_type", CARTESIAN_TYPES)
    def test_axis_config_is_present(self, chart_type):
        properties = props(chart_type)
        assert properties["dimensionAxis"]["show"] == "all"
        assert properties["measureAxis"]["show"] == "all"

    @pytest.mark.parametrize("chart_type", NEBULA_TYPES)
    def test_legend_color_and_tooltip_are_present(self, chart_type):
        properties = props(chart_type)
        for key in ("legend", "color", "tooltip", "dataPoint"):
            assert key in properties, f"{chart_type} is missing {key}"

    def test_bar_chart_orientation(self):
        assert props("barchart")["orientation"] == "vertical"

    def test_line_chart_type(self):
        assert props("linechart")["lineType"] == "line"


class TestPieChart:
    """sn-pie-chart silently ignores keys it doesn't know, so a wrong shape
    draws an empty box instead of failing the way bar/line do."""

    def test_uses_donut_not_slice(self):
        properties = props("piechart")
        assert "donut" in properties
        assert "slice" not in properties

    def test_uses_the_pie_specific_datapoint_shape(self):
        data_point = props("piechart")["dataPoint"]
        assert set(data_point) == {"auto", "labelMode", "labelValueMode"}

    def test_styles_its_slices(self):
        components = props("piechart")["components"]
        assert any(c.get("key") == "slices" for c in components)

    def test_does_not_get_cartesian_axis_config(self):
        assert "dimensionAxis" not in props("piechart")


class TestTable:
    """sn-table walks `columnOrder` to render its body. Without it the table
    draws its title and an empty white pane - the data is there, nothing is
    shown. Same class of failure as the pie chart."""

    def table_props(self, dimensions=1, measures=1):
        hypercube = {
            "qDimensions": [{"qDef": {}}] * dimensions,
            "qMeasures": [{"qDef": {}}] * measures,
        }
        return build_properties("table", "TAB_1", "Sales by Order", hypercube), hypercube

    def test_declares_column_order(self):
        properties, _ = self.table_props()
        assert properties["columnOrder"] == [0, 1]

    def test_column_widths_match_the_column_count(self):
        properties, _ = self.table_props(dimensions=2, measures=2)
        assert properties["columnOrder"] == [0, 1, 2, 3]
        assert properties["columnWidths"] == [-1] * 4  # -1 = auto

    def test_sets_the_hypercube_column_order_too(self):
        """The property-level list drives rendering; the hypercube-level one
        drives which columns the engine actually returns."""
        _, hypercube = self.table_props()
        assert hypercube["qColumnOrder"] == [0, 1]
        assert hypercube["qMode"] == "S"

    def test_has_the_table_chrome(self):
        properties, _ = self.table_props()
        for key in ("totals", "scrolling", "multiline", "search"):
            assert key in properties, f"table is missing {key}"

    def test_does_not_get_cartesian_axis_config(self):
        properties, _ = self.table_props()
        assert "dimensionAxis" not in properties

    def test_does_not_clobber_a_caller_supplied_sort_order(self):
        hypercube = {
            "qDimensions": [{"qDef": {}}],
            "qMeasures": [{"qDef": {}}],
            "qInterColumnSortOrder": [1, 0],
        }
        build_properties("table", "TAB_1", "t", hypercube)
        assert hypercube["qInterColumnSortOrder"] == [1, 0]


class TestTemplateIsolation:
    """The property trees are module-level literals shared by every call."""

    def test_mutating_one_chart_does_not_affect_the_next(self):
        first = props("barchart")
        first["legend"]["show"] = False
        first["components"].append({"key": "injected"})

        second = props("barchart")
        assert second["legend"]["show"] is True
        assert second["components"] == []

    def test_pie_components_are_not_shared(self):
        props("piechart")["components"][0]["style"]["innerRadius"] = 0.99
        assert props("piechart")["components"][0]["style"]["innerRadius"] == 0.55
