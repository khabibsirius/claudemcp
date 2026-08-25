"""Chart sorting and top-N.

Charts were coming out in data-load order, which is why a "Sales by Region"
bar chart was a row of bars in no discernible sequence, and why a chart
titled "Top 5 Products" showed every product there was.
"""

import pytest


def hypercube(engine, chart_type, dimension="Region", expression="Sum([Sales])", limit=None):
    return engine._build_hypercube(chart_type, dimension, "Sales", expression, limit=limit)


class TestMeasureSorting:

    @pytest.mark.parametrize("chart_type", ["barchart", "piechart", "sn-table"])
    def test_sorted_descending_by_the_measure(self, offline_engine, chart_type):
        cube = hypercube(offline_engine, chart_type)
        assert cube["qMeasures"][0]["qSortBy"] == {"qSortByNumeric": -1}

    @pytest.mark.parametrize("chart_type", ["barchart", "piechart", "sn-table"])
    def test_the_measure_column_sorts_first(self, offline_engine, chart_type):
        """The sort only applies if the measure leads qInterColumnSortOrder."""
        cube = hypercube(offline_engine, chart_type)
        assert cube["qInterColumnSortOrder"][0] == 1

    def test_line_charts_sort_along_the_dimension(self, offline_engine):
        """A trend line sorted by value is not a trend line."""
        cube = hypercube(offline_engine, "linechart", dimension="Order Date")

        assert "qSortBy" not in cube["qMeasures"][0]
        assert cube["qDimensions"][0]["qDef"]["qSortCriterias"] == [
            {"qSortByNumeric": 1, "qSortByAscii": 1}
        ]
        assert cube["qInterColumnSortOrder"] == [0, 1]

    def test_a_kpi_needs_no_sorting(self, offline_engine):
        cube = offline_engine._build_hypercube("kpi", None, "Sales", "Sum([Sales])")
        assert cube["qDimensions"] == []


class TestTopN:
    """Qlik cannot rank inside an expression, so top-N is a dimension limit."""

    def test_limit_becomes_a_dimension_limit(self, offline_engine):
        cube = hypercube(offline_engine, "barchart", limit=5)
        spec = cube["qDimensions"][0]["qOtherTotalSpec"]

        assert spec["qOtherMode"] == "OTHER_COUNTED"
        assert spec["qOtherCounted"] == {"qv": "5"}
        assert spec["qOtherSortMode"] == "OTHER_SORT_DESCENDING"

    def test_it_sits_beside_qdef_not_inside_it(self, offline_engine):
        """Nested in qDef the engine silently ignores it, and the chart shows
        every value - verified against a live app."""
        cube = hypercube(offline_engine, "barchart", limit=5)
        assert "qOtherTotalSpec" not in cube["qDimensions"][0]["qDef"]

    def test_the_others_bucket_is_hidden(self, offline_engine):
        """An "Others" bar aggregating everything else dwarfs the top five."""
        cube = hypercube(offline_engine, "barchart", limit=5)
        assert cube["qDimensions"][0]["qOtherTotalSpec"]["qSuppressOther"] is True

    def test_no_limit_means_no_restriction(self, offline_engine):
        cube = hypercube(offline_engine, "barchart")
        assert "qOtherTotalSpec" not in cube["qDimensions"][0]

    def test_line_charts_are_not_limited(self, offline_engine):
        """Truncating a time series to its five biggest points is meaningless."""
        cube = hypercube(offline_engine, "linechart", limit=5)
        assert "qOtherTotalSpec" not in cube["qDimensions"][0]


class TestLimitNormalisation:

    @pytest.mark.parametrize("raw,expected", [
        (5, 5), ("5", 5), (0, None), (-1, None), (None, None),
        ("", None), ("abc", None), (500, None),
    ])
    def test_limits_are_cleaned(self, raw, expected):
        from dashboard_builder import normalize_visualization

        viz = normalize_visualization({
            "type": "barchart", "title": "t", "dimension": "Region",
            "measure_expression": "Sum([Sales])", "limit": raw,
        })
        assert viz["limit"] == expected


class TestValueLabels:

    def test_bars_show_their_values(self):
        """A bar you have to measure against an axis by eye isn't telling
        you the number."""
        from chart_specs import build_properties

        properties = build_properties("barchart", "B_1", "t", {"qDimensions": [], "qMeasures": []})
        assert properties["dataPoint"]["showLabels"] is True

    def test_pie_keeps_its_own_label_shape(self):
        """sn-pie-chart ignores bar/line's dataPoint keys entirely."""
        from chart_specs import build_properties

        properties = build_properties("piechart", "P_1", "t", {"qDimensions": [], "qMeasures": []})
        assert "labelMode" in properties["dataPoint"]


class TestCreateChartPassesLimit:

    def test_limit_reaches_the_hypercube(self, engine):
        engine.ws.handlers = {
            "CreateChild": {"qReturn": {"qHandle": 9}},
            "GetProperties": {"qProp": {"cells": []}},
            "SetProperties": {},
        }
        engine.sheet_handle = 5

        engine.create_chart(
            "barchart", "Top 5 products",
            dimension="Product Name", measure_expression="Sum([Sales])", limit=5,
        )

        sent = engine.ws.requests_for("CreateChild")[0]["params"][0]
        spec = sent["qHyperCubeDef"]["qDimensions"][0]["qOtherTotalSpec"]
        assert spec["qOtherCounted"] == {"qv": "5"}
