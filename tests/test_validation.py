"""Spec validation - the layer that stops hallucinated fields reaching Qlik.

Qlik accepts a chart referencing a field that doesn't exist: it creates the
object and renders an empty box. Every check here exists because the failure
it prevents is silent.
"""

import pytest

from dashboard_builder import (
    effective_expression,
    expression_field,
    normalize_visualization,
    validate_spec,
    validate_visualization,
)
from qlik_engine import bare_field_name

FIELDS = {"Sales", "Region", "Customer Segment", "Order Date", "Order Id"}


def viz(**overrides):
    spec = {
        "type": "barchart",
        "title": "Sales by region",
        "dimension": "Region",
        "measure": "Sales",
        "measure_expression": "Sum([Sales])",
    }
    spec.update(overrides)
    return normalize_visualization(spec)


class TestBareFieldName:

    @pytest.mark.parametrize("given,expected", [
        ("[Customer Segment]", "Customer Segment"),
        ("Customer Segment", "Customer Segment"),
        ("  [Region]  ", "Region"),
        ("", ""),
        (None, None),
    ])
    def test_strips_quoting_brackets(self, given, expected):
        assert bare_field_name(given) == expected

    def test_leaves_calculated_dimensions_alone(self):
        assert bare_field_name("=Year([Order Date])") == "=Year([Order Date])"


class TestNormalization:

    def test_bracketed_dimension_is_unwrapped(self):
        assert viz(dimension="[Customer Segment]")["dimension"] == "Customer Segment"

    def test_bracketed_measure_is_unwrapped(self):
        """Left bracketed, the Sum([...]) fallback would build Sum([[Sales]])."""
        assert viz(type="kpi", measure="[Sales]")["measure"] == "Sales"

    def test_kpi_dimension_is_dropped(self):
        """A KPI is one aggregate with no grouping; a dimension would turn it
        into a one-column table."""
        assert viz(type="kpi", dimension="Region")["dimension"] is None

    def test_type_is_lowercased(self):
        assert viz(type="BarChart")["type"] == "barchart"


class TestEffectiveExpression:

    def test_uses_the_explicit_expression(self):
        assert effective_expression(viz()) == "Sum([Sales])"

    def test_falls_back_to_sum_of_the_measure(self):
        """Mirrors QlikEngine.create_chart, so validation checks what will
        actually be sent rather than what was written down."""
        assert effective_expression(viz(measure_expression="")) == "Sum([Sales])"

    def test_empty_when_there_is_nothing_to_aggregate(self):
        assert effective_expression(viz(measure="", measure_expression="")) == ""

    def test_expression_field_extraction(self):
        assert expression_field("Count(DISTINCT [Order Id])") == "Order Id"
        assert expression_field("Sum(Sales)") is None


class TestVisualizationValidation:

    def test_accepts_a_good_spec(self):
        assert validate_visualization(viz(), FIELDS) is None

    def test_rejects_unknown_chart_type(self):
        assert "unknown chart type" in validate_visualization(
            viz(type="definitely-not-a-chart"), FIELDS
        )

    def test_scatter_is_a_real_type_now_but_needs_two_measures(self):
        """It used to be rejected as unknown. It exists, and the reason it
        never worked is that it needs two measures."""
        error = validate_visualization(viz(type="scatter"), FIELDS)
        assert "needs 2 measures" in error

    def test_rejects_missing_title(self):
        assert "title" in validate_visualization(viz(title=""), FIELDS)

    def test_rejects_invented_dimension(self):
        error = validate_visualization(viz(dimension="Regionn"), FIELDS)
        assert "not a field in this app" in error

    def test_rejects_invented_measure_field(self):
        error = validate_visualization(viz(measure_expression="Sum([Revenue])"), FIELDS)
        assert "unknown field" in error

    def test_accepts_a_bracketed_dimension_that_is_real(self):
        """Normalization has to run before the field check, or a correct
        field name fails purely because the model bracketed it."""
        assert validate_visualization(viz(dimension="[Customer Segment]"), FIELDS) is None

    def test_dimensional_charts_need_a_dimension(self):
        error = validate_visualization(viz(dimension=""), FIELDS)
        assert "needs a dimension" in error

    @pytest.mark.parametrize("expression", [
        "Sum([Sales]) SortBy Sum([Sales]) DESC",
        "Aggr(Sum([Sales]), [Region])",
        "Sum([Sales]) / Count([Order Id])",
        "Rank(Sum([Sales]))",
        "Sum([Sales]",
    ])
    def test_rejects_non_simple_expressions(self, expression):
        error = validate_visualization(viz(measure_expression=expression), FIELDS)
        assert error is not None, f"{expression!r} should have been rejected"

    @pytest.mark.parametrize("expression", [
        "Sum([Sales])",
        "Count([Order Id])",
        "Count(DISTINCT [Order Id])",
        "Avg([Sales])",
        "min([Sales])",
        "MAX( [Sales] )",
    ])
    def test_accepts_simple_aggregations(self, expression):
        assert validate_visualization(viz(measure_expression=expression), FIELDS) is None

    def test_skips_field_checks_when_no_field_list_is_given(self):
        assert validate_visualization(viz(dimension="Anything"), None) is None


class TestKpiValidationRegression:
    """A KPI with no measure_expression used to skip validation entirely.

    The guard read `if viz_type != "kpi" or viz.get("measure_expression")`,
    which is falsy for exactly that case - so the spec went straight to Qlik,
    where create_chart built Sum([<unchecked field>]) and produced a silently
    blank KPI.
    """

    def test_kpi_without_expression_is_still_field_checked(self):
        spec = viz(type="kpi", measure="Revenue", measure_expression="")
        error = validate_visualization(spec, FIELDS)
        assert error is not None
        assert "unknown field" in error

    def test_kpi_without_expression_passes_when_the_field_is_real(self):
        spec = viz(type="kpi", measure="Sales", measure_expression="")
        assert validate_visualization(spec, FIELDS) is None

    def test_kpi_with_neither_measure_nor_expression_is_rejected(self):
        spec = viz(type="kpi", measure="", measure_expression="")
        assert "no measure" in validate_visualization(spec, FIELDS)


class TestSpecValidation:

    def test_accepts_a_well_formed_spec(self):
        spec = {"dashboard_title": "Sales", "visualizations": [viz()]}
        assert validate_spec(spec) is spec

    @pytest.mark.parametrize("spec", [
        {},
        {"dashboard_title": "Sales"},
        {"dashboard_title": "", "visualizations": [{}]},
        {"dashboard_title": "Sales", "visualizations": []},
        {"dashboard_title": "Sales", "visualizations": "not a list"},
        {"dashboard_title": "Sales", "visualizations": ["a string"]},
        [],
    ])
    def test_rejects_malformed_specs(self, spec):
        with pytest.raises(ValueError):
            validate_spec(spec)
