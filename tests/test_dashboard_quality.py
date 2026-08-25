"""Dashboard quality: layout order, duplicates, and useless dimensions.

A dashboard that renders is not the same as a dashboard worth looking at.
These are the checks that turn "technically five charts" into something a
person can read.
"""

import pytest

from dashboard_builder import arrange, build_dashboard, normalize_visualization, validate_visualization

FIELDS = [
    {"name": "Sales", "cardinality": 193, "tags": ["$numeric"]},
    {"name": "Market", "cardinality": 5, "tags": ["$text"]},
    {"name": "Customer Segment", "cardinality": 3, "tags": ["$text"]},
    {"name": "Order Id", "cardinality": 65752, "tags": ["$key"]},
    {"name": "Country", "cardinality": 1, "tags": ["$text"]},
]


def viz(type_, title, dimension="", expression="Sum([Sales])"):
    return {
        "type": type_, "title": title, "dimension": dimension,
        "measure": "Sales", "measure_expression": expression,
    }


class RecordingEngine:
    """Captures what would be created, without touching Qlik."""

    sheet_id = "SH_test"

    def __init__(self):
        self.charts = []

    def create_sheet(self, title, description="Created by AI"):
        self.title = title

    def create_chart(self, chart_type, title, **kwargs):
        self.charts.append((chart_type, title, kwargs.get("dimension")))

    def save(self):
        pass


class TestLayoutOrder:
    """Charts are placed in build order, so order is the layout."""

    def test_kpis_come_first(self):
        ordered = arrange([viz("barchart", "b"), viz("kpi", "k"), viz("table", "t")])
        assert [v["type"] for v in ordered] == ["kpi", "barchart", "table"]

    def test_tables_come_last(self):
        """A table is full width; anything after it is pushed off the fold."""
        ordered = arrange([viz("table", "t"), viz("piechart", "p"), viz("kpi", "k")])
        assert ordered[-1]["type"] == "table"

    def test_order_is_stable_within_a_type(self):
        ordered = arrange([viz("kpi", "first"), viz("kpi", "second"), viz("kpi", "third")])
        assert [v["title"] for v in ordered] == ["first", "second", "third"]

    def test_unknown_types_are_not_dropped(self):
        assert len(arrange([viz("scatter", "s"), viz("kpi", "k")])) == 2

    def test_a_realistic_spec_reads_top_down(self):
        ordered = arrange([
            viz("table", "Detail"), viz("barchart", "By market"),
            viz("kpi", "Total"), viz("piechart", "Share"), viz("kpi", "Average"),
        ])
        assert [v["type"] for v in ordered] == [
            "kpi", "kpi", "barchart", "piechart", "table",
        ]


class TestDuplicateRejection:
    """Models cheerfully chart the same number three ways."""

    def build(self, visualizations):
        engine = RecordingEngine()
        spec = {"dashboard_title": "D", "visualizations": visualizations}
        return engine, build_dashboard(engine, spec, fields=FIELDS)

    def test_identical_charts_are_built_once(self):
        engine, (built, skipped) = self.build([
            viz("barchart", "Sales by market", "Market"),
            viz("barchart", "Market sales", "Market"),
        ])
        assert len(built) == 1
        assert len(engine.charts) == 1
        assert "duplicate" in skipped[0][1]

    def test_the_same_data_in_a_different_chart_type_is_kept(self):
        """A KPI and a bar chart of the same measure are not redundant."""
        _, (built, _) = self.build([
            viz("kpi", "Total sales"),
            viz("barchart", "Sales by market", "Market"),
        ])
        assert len(built) == 2

    def test_same_dimension_different_measure_is_kept(self):
        _, (built, _) = self.build([
            viz("barchart", "Sales by market", "Market", "Sum([Sales])"),
            viz("barchart", "Orders by market", "Market", "Count([Order Id])"),
        ])
        assert len(built) == 2

    def test_the_same_chart_with_a_different_limit_is_kept(self):
        """The regression: "Top 5 markets" was skipped as a duplicate of
        the all-markets chart because the signature ignored the limit."""
        _, (built, _) = self.build([
            viz("barchart", "Sales by market", "Market"),
            dict(viz("barchart", "Top 5 markets", "Market"), limit=5),
        ])
        assert len(built) == 2

    def test_the_same_chart_in_a_different_colour_is_kept(self):
        _, (built, _) = self.build([
            dict(viz("barchart", "Sales by market", "Market"), color="red"),
            dict(viz("barchart", "Sales by market, in blue", "Market"), color="blue"),
        ])
        assert len(built) == 2


class TestUselessDimensions:

    def test_a_constant_dimension_is_rejected(self):
        """Grouping by a field with one value draws exactly one bar."""
        spec = normalize_visualization(viz("barchart", "By country", "Country"))
        error = validate_visualization(
            spec, {f["name"] for f in FIELDS},
            field_info={f["name"]: f for f in FIELDS},
        )
        assert error is not None
        assert "same value in every row" in error

    def test_an_identifier_dimension_is_still_rejected(self):
        spec = normalize_visualization(viz("table", "By order", "Order Id"))
        error = validate_visualization(
            spec, {f["name"] for f in FIELDS},
            field_info={f["name"]: f for f in FIELDS},
        )
        assert "too many to group by" in error

    @pytest.mark.parametrize("dimension", ["Market", "Customer Segment"])
    def test_real_categories_are_accepted(self, dimension):
        spec = normalize_visualization(viz("barchart", "t", dimension))
        assert validate_visualization(
            spec, {f["name"] for f in FIELDS},
            field_info={f["name"]: f for f in FIELDS},
        ) is None


class TestBuiltSheetShape:

    def test_a_messy_spec_becomes_an_ordered_deduplicated_sheet(self):
        engine = RecordingEngine()
        spec = {"dashboard_title": "Sales", "visualizations": [
            viz("table", "Everything by order", "Order Id"),      # identifier
            viz("barchart", "Sales by market", "Market"),
            viz("barchart", "Market sales again", "Market"),      # duplicate
            viz("kpi", "Total sales"),
            viz("piechart", "By country", "Country"),             # constant
            viz("piechart", "Share by segment", "Customer Segment"),
        ]}

        built, skipped = build_dashboard(engine, spec, fields=FIELDS)

        assert [c[0] for c in engine.charts] == ["kpi", "barchart", "piechart"]
        assert len(built) == 3
        assert len(skipped) == 3
