import pytest

from chart_defaults import CHART_DEFAULTS
from chart_specs import (
    CARTESIAN_TYPES,
    CHART_TYPES,
    DEFAULT_SIZES,
    NEBULA_TYPES,
    build_properties,
    hypercube_owner,
    default_size,
    resolve_chart_type,
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
        properties = props(chart_type)
        assert properties["visualization"] == properties["qInfo"]["qType"]

    @pytest.mark.parametrize("chart_type", CHART_TYPES)
    def test_carries_the_hypercube(self, chart_type):
        owner, _path = hypercube_owner(props(chart_type))
        assert "qHyperCubeDef" in owner

    @pytest.mark.parametrize("chart_type", CHART_TYPES)
    def test_the_dimensions_reach_the_cube_the_component_reads(self, chart_type):
        cube = {"qDimensions": [{"qDef": {"qFieldDefs": ["Region"]}}],
                "qMeasures": [{"qDef": {"qDef": "=Sum([Sales])"}}]}
        properties = build_properties(chart_type, "OBJ_1", "t", cube)
        owner, _path = hypercube_owner(properties)
        assert owner["qHyperCubeDef"]["qDimensions"] == cube["qDimensions"]
        assert owner["qHyperCubeDef"]["qMeasures"] == cube["qMeasures"]

    def test_only_the_box_plot_nests_its_cube(self):
        nested = [
            t for t in CHART_TYPES
            if hypercube_owner(props(t))[1] != "qHyperCubeDef"
        ]
        assert nested == ["boxplot"]

    @pytest.mark.parametrize("chart_type", CHART_TYPES)
    def test_has_a_default_size(self, chart_type):
        colspan, rowspan = default_size(chart_type)
        assert 1 <= colspan <= 24 and rowspan >= 1


class TestNebulaRequirements:
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
    def table_props(self, dimensions=1, measures=1, name="table"):
        hypercube = {
            "qDimensions": [{"qDef": {}}] * dimensions,
            "qMeasures": [{"qDef": {}}] * measures,
        }
        return build_properties(
            resolve_chart_type(name), "TAB_1", "Sales by Order", hypercube
        ), hypercube

    def test_the_word_table_builds_an_sn_table(self):
        assert resolve_chart_type("table") == "sn-table"

    def test_the_object_is_typed_as_sn_table(self):
        properties, _ = self.table_props()
        assert properties["qInfo"]["qType"] == "sn-table"
        assert properties["visualization"] == "sn-table"

    def test_declares_column_order(self):
        properties, _ = self.table_props()
        assert properties["qHyperCubeDef"]["qColumnOrder"] == [0, 1]

    def test_column_widths_match_the_column_count(self):
        properties, _ = self.table_props(dimensions=2, measures=2)
        assert properties["qHyperCubeDef"]["qColumnOrder"] == [0, 1, 2, 3]
        assert properties["columnWidths"] == [-1] * 4

    def test_carries_the_bundle_version_not_a_pinned_one(self):
        properties, _ = self.table_props()
        assert properties["version"] == CHART_DEFAULTS["sn-table"]["version"]

    def test_has_the_table_chrome(self):
        properties, _ = self.table_props()
        for key in ("totals", "usePagination", "components"):
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
        properties = build_properties("sn-table", "TAB_1", "t", hypercube)
        assert properties["qHyperCubeDef"]["qInterColumnSortOrder"] == [1, 0]

    def test_the_callers_hypercube_is_not_mutated(self):
        hypercube = {"qDimensions": [{"qDef": {}}], "qMeasures": [{"qDef": {}}]}
        build_properties("table", "TAB_1", "t", hypercube)
        assert set(hypercube) == {"qDimensions", "qMeasures"}


class TestTemplateIsolation:
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
