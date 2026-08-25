"""Charts that are well-formed and still show nothing.

Every other check in dashboard_builder asks whether a chart is well FORMED:
the field exists, the expression parses, the dimension is a category rather
than an id. A chart can pass all of it and still render an empty box, because
"valid" and "returns data" are different questions. Sum([Branch Name]) is a
real aggregation over a real field that totals nothing; a set analysis can
select down to no rows.

That empty box is worse than a refusal - the person is told the chart was
built, and finds out weeks later it never showed anything. So the chart is
queried against the live app before it is committed, and what it returns is
handed back to the caller so the reply describes contents rather than titles.
"""

import pytest

from chat_tools import _chart_preview, create_chart
from dashboard_builder import (
    build_sheet,
    probe_visualization,
    validate_visualization,
)

FIELDS = [
    {"name": "Deposits", "cardinality": 4210, "tags": ["$numeric"]},
    {"name": "Region", "cardinality": 6, "tags": ["$text"]},
    {"name": "Branch Name", "cardinality": 11, "tags": ["$text"]},
]


def viz(title="Deposits by region", dimension="Region",
        expression="Sum([Deposits])", type_="barchart"):
    return {
        "type": type_, "title": title, "dimension": dimension,
        "measure": "Deposits", "measure_expression": expression,
    }


class ProbeEngine:
    """An engine whose query results are dictated by the test.

    Keyed by expression, because the whole point of the probe is that two
    expressions which both pass check_expression return different things.
    """

    sheet_id = "SH_test"
    sheet_handle = 1  # a sheet is already open, as it is after a build

    def __init__(self, results=None):
        self.results = results or {}
        self.charts = []
        self.queries = []

    # -- what the probe consults ---------------------------------------

    def check_expression(self, expression):
        return {"valid": True, "error": "", "bad_fields": [], "expression": expression}

    def query(self, dimensions=None, measures=None, limit=50, sort_by_measure=True):
        self.queries.append((tuple(dimensions or []), tuple(measures or [])))
        expression = (measures or [""])[0]
        if expression not in self.results:
            # Default: a healthy chart, so a test only has to describe the
            # broken case it is actually about.
            return {
                "columns": ["Region", expression],
                "rows": [
                    {"Region": "г.Алматы", expression: 90049825.0},
                    {"Region": "Астана", expression: 20100000.0},
                ],
                "returned_rows": 2,
                "total_rows": 6,
            }
        return self.results[expression]

    def get_fields(self):
        return FIELDS

    # -- what building consults ----------------------------------------

    def create_sheet(self, title, description="Created by AI"):
        self.title = title

    def create_chart(self, chart_type, title, **kwargs):
        self.charts.append((chart_type, title))

    def open_sheet(self, title):
        self.title = title

    def save(self):
        pass


EMPTY = {"columns": ["Region", "Sum([Deposits])"], "rows": [],
         "returned_rows": 0, "total_rows": 0}


class TestAnEmptyChartIsRefused:
    """The check that was missing: valid, and still nothing to look at."""

    def test_no_rows_is_rejected(self):
        engine = ProbeEngine({"Sum([Deposits])": EMPTY})
        error = validate_visualization(viz(), {"Deposits", "Region"}, engine=engine)
        assert error and "no rows" in error

    def test_the_reason_says_it_would_render_blank(self):
        """The model acts on the reason, so it has to name the symptom."""
        engine = ProbeEngine({"Sum([Deposits])": EMPTY})
        error = validate_visualization(viz(), {"Deposits", "Region"}, engine=engine)
        assert "blank" in error

    def test_a_text_field_aggregation_is_rejected(self):
        """Sum of a name is valid Qlik and draws nothing."""
        engine = ProbeEngine({
            "Sum([Branch Name])": {
                "columns": ["Region", "Sum([Branch Name])"],
                "rows": [{"Region": "г.Алматы", "Sum([Branch Name])": "-"}],
                "returned_rows": 1, "total_rows": 6,
            },
        })
        error = validate_visualization(
            viz(expression="Sum([Branch Name])"), {"Branch Name", "Region"},
            engine=engine,
        )
        assert error and "no numeric value" in error

    def test_a_chart_with_data_passes(self):
        engine = ProbeEngine()
        assert validate_visualization(
            viz(), {"Deposits", "Region"}, engine=engine
        ) is None


class TestTheProbeIsBestEffort:
    """A chart is not worth refusing because the lookup itself failed."""

    def test_a_failing_query_does_not_reject_the_chart(self):
        class Broken(ProbeEngine):
            def query(self, **kwargs):
                raise RuntimeError("engine went away")

        assert validate_visualization(
            viz(), {"Deposits", "Region"}, engine=Broken()
        ) is None

    def test_no_engine_means_no_probe(self):
        preview, problem = probe_visualization(None, viz())
        assert (preview, problem) == (None, None)

    @pytest.mark.parametrize("type_", ["filterpane", "histogram", "qlik-word-cloud"])
    def test_types_that_aggregate_nothing_are_not_probed(self, type_):
        """There is no measure to read back, so there is nothing to call empty."""
        engine = ProbeEngine({"Sum([Deposits])": EMPTY})
        preview, problem = probe_visualization(engine, viz(type_=type_))
        assert (preview, problem) == (None, None)
        assert engine.queries == []


class TestThePreviewSaysWhatWasBuilt:
    """Type and title describe a chart nobody has seen the inside of."""

    def test_a_grouped_chart_reports_its_largest_category(self):
        preview, _ = probe_visualization(ProbeEngine(), viz())
        assert preview["largest"] == {"label": "г.Алматы", "value": 90049825.0}

    def test_it_reports_how_many_categories_there_are(self):
        preview, _ = probe_visualization(ProbeEngine(), viz())
        assert preview["categories"] == 6

    def test_a_kpi_reports_its_value(self):
        engine = ProbeEngine({
            "Sum([Deposits])": {
                "columns": ["Sum([Deposits])"],
                "rows": [{"Sum([Deposits])": 121092479.0}],
                "returned_rows": 1, "total_rows": 1,
            },
        })
        preview, _ = probe_visualization(
            engine, viz(type_="kpi", dimension=None)
        )
        assert preview["value"] == 121092479.0

    def test_an_all_zero_chart_is_built_but_flagged(self):
        """Zero can be real, so it is reported rather than refused."""
        engine = ProbeEngine({
            "Sum([Deposits])": {
                "columns": ["Region", "Sum([Deposits])"],
                "rows": [{"Region": "г.Алматы", "Sum([Deposits])": 0.0}],
                "returned_rows": 1, "total_rows": 6,
            },
        })
        preview, problem = probe_visualization(engine, viz())
        assert problem is None
        assert "warning" in preview

    def test_the_probe_runs_once_per_chart(self):
        """design_full_dashboard validates, then build_dashboard validates
        again. A probe is a real query, so the second pass reuses the first."""
        engine = ProbeEngine()
        spec = viz()
        validate_visualization(spec, {"Deposits", "Region"}, engine=engine)
        validate_visualization(spec, {"Deposits", "Region"}, engine=engine)
        assert len(engine.queries) == 1


class TestWhatTheCallerIsTold:
    """The tool result is the model's only evidence of what it made."""

    def test_built_charts_carry_what_they_show(self):
        engine = ProbeEngine()
        result = build_sheet(engine, "Overview", [viz()], fields=FIELDS)
        assert result["built"][0]["shows"]["largest"]["label"] == "г.Алматы"

    def test_an_empty_chart_is_reported_as_skipped_not_built(self):
        engine = ProbeEngine({"Sum([Deposits])": EMPTY})
        result = build_sheet(engine, "Overview", [viz()], fields=FIELDS)
        assert result["built"] == []
        assert "no rows" in result["skipped"][0]["reason"]

    def test_an_empty_chart_never_reaches_qlik(self):
        engine = ProbeEngine({"Sum([Deposits])": EMPTY})
        build_sheet(engine, "Overview", [viz()], fields=FIELDS)
        assert engine.charts == []


class TestCreateChartChecksTheSameThing:
    """create_chart builds the multi-dimension types and had the same hole."""

    def test_an_empty_chart_is_refused(self):
        engine = ProbeEngine({"Sum([Deposits])": EMPTY})
        result = create_chart(
            engine, "barchart", "Deposits by region",
            dimensions=["Region"], measures=["Sum([Deposits])"],
        )
        assert result["created"] is False
        assert "render blank" in result["error"]
        assert engine.charts == []

    def test_a_chart_with_data_reports_what_it_shows(self):
        engine = ProbeEngine()
        result = create_chart(
            engine, "barchart", "Deposits by region",
            dimensions=["Region"], measures=["Sum([Deposits])"],
        )
        assert result["created"]["shows"]["largest"]["label"] == "г.Алматы"

    def test_a_multi_dimension_chart_is_probed_on_every_dimension(self):
        """The cube the chart runs is the only cube worth testing.

        Two dimensions that each hold data can still return nothing when
        combined, and that combination is what the chart draws.
        """
        engine = ProbeEngine()
        _chart_preview(
            engine, "qlik-sankey-chart-ext",
            ["Region", "Branch Name"], ["Sum([Deposits])"],
        )
        assert engine.queries == [
            (("Region", "Branch Name"), ("Sum([Deposits])",))
        ]

    def test_a_second_measure_that_returns_nothing_is_caught(self):
        """The scatter plot that reported real numbers and drew no points.

        Probing only the first measure passes a chart whose Y axis is empty:
        X is perfect, every point is missing, and the chart renders blank.
        """
        engine = ProbeEngine({
            "Sum([Deposits])": {
                "columns": ["Branch Name", "Sum([Deposits])", "Sum([Staff])"],
                "rows": [
                    {"Branch Name": "Отделение №81 Алматы",
                     "Sum([Deposits])": 1482699.0, "Sum([Staff])": "-"},
                ],
                "returned_rows": 1, "total_rows": 180,
            },
        })
        preview, problem = _chart_preview(
            engine, "scatterplot", ["Branch Name"],
            ["Sum([Deposits])", "Sum([Staff])"],
        )
        assert preview is None
        assert problem and "Sum([Staff])" in problem

    def test_both_measures_reach_the_probe(self):
        engine = ProbeEngine()
        _chart_preview(
            engine, "scatterplot", ["Branch Name"],
            ["Sum([Deposits])", "Sum([Staff])"],
        )
        assert engine.queries == [
            (("Branch Name",), ("Sum([Deposits])", "Sum([Staff])"))
        ]

    def test_the_preview_quotes_the_first_measure_not_the_last(self):
        """A chart is read by its primary measure."""
        engine = ProbeEngine({
            "Sum([Deposits])": {
                "columns": ["Branch Name", "Sum([Deposits])", "Sum([Staff])"],
                "rows": [
                    {"Branch Name": "Отделение №81 Алматы",
                     "Sum([Deposits])": 1482699.0, "Sum([Staff])": 12.0},
                ],
                "returned_rows": 1, "total_rows": 180,
            },
        })
        preview, _ = _chart_preview(
            engine, "scatterplot", ["Branch Name"],
            ["Sum([Deposits])", "Sum([Staff])"],
        )
        assert preview["largest"]["value"] == 1482699.0
        assert preview["measures"] == ["Sum([Deposits])", "Sum([Staff])"]
