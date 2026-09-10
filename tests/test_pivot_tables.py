import pytest

from chart_specs import (
    CROSS_TAB_TYPES,
    chart_requirements,
    describe_chart_types,
)
from dashboard_builder import normalize_visualization, validate_visualization
from qlik_engine import QlikEngine, QlikEngineError

FIELDS = {"Deposits", "Region", "Year", "Product"}


def viz(dimension="Region", type_="sn-pivot-table"):
    return {
        "type": type_, "title": "Deposits by region and year",
        "dimension": dimension, "measure": "Deposits",
        "measure_expression": "Sum([Deposits])",
    }


class TestTheCatalogueStatesTheRealMinimum:
    def test_a_pivot_table_needs_two_dimensions(self):
        (low, _high), _measures = chart_requirements("sn-pivot-table")
        assert low == 2

    def test_the_catalogue_says_so(self):
        assert "sn-pivot-table (2+ dimensions" in describe_chart_types()

    def test_the_upper_bound_is_left_alone(self):
        (_low, high), _measures = chart_requirements("sn-pivot-table")
        assert high == 1000

    def test_other_types_are_untouched(self):
        from chart_defaults import CHART_DEFAULTS

        for chart_type in ("barchart", "sn-table", "scatterplot", "treemap"):
            spec = CHART_DEFAULTS[chart_type]
            assert chart_requirements(chart_type) == (
                tuple(spec["dimensions"]), tuple(spec["measures"])
            )


class TestAOneDimensionPivotIsRefused:
    def test_a_dashboard_spec_cannot_express_one(self):
        error = validate_visualization(viz(), FIELDS)
        assert error and "2 dimensions" in error

    def test_the_reason_points_at_create_chart(self):
        error = validate_visualization(viz(), FIELDS)
        assert "create_chart" in error

    def test_a_table_with_one_dimension_is_still_fine(self):
        spec = normalize_visualization(viz(type_="table"))
        assert spec["type"] == "sn-table"
        assert validate_visualization(spec, FIELDS) is None


class TestTheDimensionsAreSplitBetweenTheAxes:
    def cube(self, dimensions, chart_type="sn-pivot-table"):
        return QlikEngine._build_hypercube(
            QlikEngine, chart_type, dimensions, None, ["Sum([Deposits])"]
        )

    def test_two_dimensions_put_one_on_each_axis(self):
        assert self.cube(["Region", "Year"])["qNoOfLeftDims"] == 1

    def test_three_dimensions_keep_the_last_on_top(self):
        assert self.cube(["Region", "Product", "Year"])["qNoOfLeftDims"] == 2

    def test_one_dimension_stays_on_the_left(self):
        assert self.cube(["Region"])["qNoOfLeftDims"] == 1

    def test_the_split_is_never_zero(self):
        for dimensions in (["Region"], ["Region", "Year"],
                           ["Region", "Product", "Year"]):
            assert self.cube(dimensions)["qNoOfLeftDims"] >= 1

    def test_other_chart_types_do_not_get_a_split(self):
        assert "qNoOfLeftDims" not in self.cube(["Region"], "barchart")
        assert "qNoOfLeftDims" not in self.cube(["Region"], "table")

    def test_pivot_mode_survives_the_merge(self):
        from chart_specs import build_properties

        properties = build_properties(
            "sn-pivot-table", "OBJ_1", "Deposits",
            self.cube(["Region", "Year"]),
        )
        cube = properties["qHyperCubeDef"]
        assert cube["qMode"] == "P"
        assert cube["qNoOfLeftDims"] == 1
        assert len(cube["qDimensions"]) == 2


class TestTheEngineRefusesToBuildOne:
    def test_create_chart_rejects_a_single_dimension_pivot(self):
        engine = QlikEngine.__new__(QlikEngine)
        engine.sheet_handle = 1
        with pytest.raises(QlikEngineError, match="at least 2 dimension"):
            engine.create_chart(
                "sn-pivot-table", "Deposits",
                dimensions=["Region"],
                measure_expressions=["Sum([Deposits])"],
            )


class TestTheCrossTabListIsShared:
    def test_the_pivot_table_is_the_cross_tab(self):
        assert CROSS_TAB_TYPES == ("sn-pivot-table",)
