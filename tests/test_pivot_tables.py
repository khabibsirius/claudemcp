"""Pivot tables: the type where "accepts" and "renders" part company.

Every other chart is a list of dimensions and a list of measures. A pivot
table is one dimension crossed against ANOTHER - a grid with row headers down
the side and column headers across the top - and Qlik's bundle does not say
so. It reports a minimum of one dimension, which is true of the object and
false of the picture: with one dimension there is nothing to lay across the
top, so the engine builds it, the client renders an empty grid, and nobody is
told anything went wrong.

Two things follow, and both are tested here. The catalogue the assistant
reads has to state the real minimum, because that catalogue is where it
learns what a pivot table is. And the hypercube has to say how the dimensions
divide between the two axes, because a split left to a default is how every
dimension ends up on one axis with nothing on the other.
"""

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
    """The generated catalogue is where the model learns the rule."""

    def test_a_pivot_table_needs_two_dimensions(self):
        (low, _high), _measures = chart_requirements("sn-pivot-table")
        assert low == 2

    def test_the_catalogue_says_so(self):
        """describe_chart_types() goes verbatim into the prompt."""
        assert "sn-pivot-table (2+ dimensions" in describe_chart_types()

    def test_the_upper_bound_is_left_alone(self):
        """Only the minimum was wrong; a pivot really does take many."""
        (_low, high), _measures = chart_requirements("sn-pivot-table")
        assert high == 1000

    def test_other_types_are_untouched(self):
        """Only the cross-tabs are corrected; everything else is the bundle's
        own answer, which is why a barchart still reports what Qlik says."""
        from chart_defaults import CHART_DEFAULTS

        for chart_type in ("barchart", "sn-table", "scatterplot", "treemap"):
            spec = CHART_DEFAULTS[chart_type]
            assert chart_requirements(chart_type) == (
                tuple(spec["dimensions"]), tuple(spec["measures"])
            )


class TestAOneDimensionPivotIsRefused:
    """A cross-tab with nothing to cross is a table, and should say so."""

    def test_a_dashboard_spec_cannot_express_one(self):
        error = validate_visualization(viz(), FIELDS)
        assert error and "2 dimensions" in error

    def test_the_reason_points_at_create_chart(self):
        """A dashboard spec carries one dimension, so the fix is the tool
        that carries more - not a different field."""
        error = validate_visualization(viz(), FIELDS)
        assert "create_chart" in error

    def test_a_table_with_one_dimension_is_still_fine(self):
        """The rule is about cross-tabs, not about single dimensions.

        Normalised first, as build_dashboard does: "table" is an alias onto
        sn-table, and validate_visualization sees canonical types only.
        """
        spec = normalize_visualization(viz(type_="table"))
        assert spec["type"] == "sn-table"
        assert validate_visualization(spec, FIELDS) is None


class TestTheDimensionsAreSplitBetweenTheAxes:
    """qNoOfLeftDims is what makes a pivot table a grid rather than a list."""

    def cube(self, dimensions, chart_type="sn-pivot-table"):
        return QlikEngine._build_hypercube(
            QlikEngine, chart_type, dimensions, None, ["Sum([Deposits])"]
        )

    def test_two_dimensions_put_one_on_each_axis(self):
        """Region down the side, Year across the top - the cross-tab."""
        assert self.cube(["Region", "Year"])["qNoOfLeftDims"] == 1

    def test_three_dimensions_keep_the_last_on_top(self):
        assert self.cube(["Region", "Product", "Year"])["qNoOfLeftDims"] == 2

    def test_one_dimension_stays_on_the_left(self):
        """Nothing on top draws a plain table; nothing on the LEFT draws
        an empty grid, so a lone dimension must not end up on top."""
        assert self.cube(["Region"])["qNoOfLeftDims"] == 1

    def test_the_split_is_never_zero(self):
        for dimensions in (["Region"], ["Region", "Year"],
                           ["Region", "Product", "Year"]):
            assert self.cube(dimensions)["qNoOfLeftDims"] >= 1

    def test_other_chart_types_do_not_get_a_split(self):
        """qNoOfLeftDims means nothing outside pivot mode."""
        assert "qNoOfLeftDims" not in self.cube(["Region"], "barchart")
        assert "qNoOfLeftDims" not in self.cube(["Region"], "table")

    def test_pivot_mode_survives_the_merge(self):
        """The bundle's qMode: 'P' is what makes it pivot at all, and it
        comes from the defaults rather than from the cube we build."""
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
    """The last line: even a caller bypassing the checks above."""

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
    """One list, so the catalogue and the hypercube cannot disagree."""

    def test_the_pivot_table_is_the_cross_tab(self):
        assert CROSS_TAB_TYPES == ("sn-pivot-table",)
