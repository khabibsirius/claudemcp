"""Charts that need more than one dimension or measure.

Asking for a sankey chart failed four times in a row. Not because the type
was missing - it was there, with the right properties - but because the only
chart-building tool the assistant had was build_dashboard, whose spec carries
exactly one dimension and one measure per chart. A sankey needs two to five
dimensions, so it could not be expressed at all, and the design pipeline
quietly substituted bar charts each time.
"""

import pytest

import chat_tools
import web_app
from chart_specs import chart_requirements
from qlik_engine import QlikEngineError, QlikNotFoundError


class RecordingEngine:
    sheet_handle = None
    sheet_id = "SH_1"

    def __init__(self, existing_sheets=()):
        self.charts = []
        self.sheets = []
        self.opened = []
        self.saved = 0
        self.existing = list(existing_sheets)

    def create_sheet(self, title, description="Created by AI"):
        self.sheets.append(title)
        self.existing.append(title)
        self.sheet_handle = 1

    def open_sheet(self, name_or_id):
        if name_or_id not in self.existing:
            raise QlikNotFoundError(f"No sheet called {name_or_id!r}.")
        self.opened.append(name_or_id)
        self.sheet_handle = 1
        return {"qId": "SH_1", "title": name_or_id, "chart_count": 3}

    def create_chart(self, chart_type, title, **kwargs):
        self.charts.append((chart_type, title, kwargs))

    def save(self):
        self.saved += 1


class TestTheGapThatCausedIt:

    def test_a_sankey_needs_more_dimensions_than_a_dashboard_spec_carries(self):
        dimensions, _ = chart_requirements("qlik-sankey-chart-ext")
        assert dimensions[0] >= 2, "a spec with one dimension can never satisfy it"

    @pytest.mark.parametrize("chart_type", [
        "qlik-sankey-chart-ext", "scatterplot", "mekkochart", "sn-grid-chart",
    ])
    def test_these_types_are_all_unreachable_from_a_single_slot_spec(self, chart_type):
        dimensions, measures = chart_requirements(chart_type)
        assert dimensions[0] > 1 or measures[0] > 1


class TestCreateChartTool:

    def test_it_exists(self):
        assert "create_chart" in chat_tools.FUNCTIONS
        names = {t["function"]["name"] for t in chat_tools.TOOLS}
        assert "create_chart" in names

    def test_the_assistant_can_use_it(self):
        """The previous advice pointed at qlik_build_sheet, which is an MCP
        tool the chatbot does not have - so it was unfollowable."""
        assert "create_chart" in web_app.ASSISTANT_TOOLS

    def test_it_passes_every_dimension_through(self):
        engine = RecordingEngine()
        chat_tools.create_chart(
            engine, "qlik-sankey-chart-ext", "Flow",
            dimensions=["Currency", "Region", "Type"], measures=["Sum([SUM])"],
        )

        _, _, kwargs = engine.charts[0]
        assert kwargs["dimensions"] == ["Currency", "Region", "Type"]
        assert kwargs["measure_expressions"] == ["Sum([SUM])"]

    def test_it_passes_every_measure_through(self):
        engine = RecordingEngine()
        chat_tools.create_chart(
            engine, "scatterplot", "Spread",
            dimensions=["Region"], measures=["Sum([SUM])", "Count([agent])"],
        )
        assert engine.charts[0][2]["measure_expressions"] == [
            "Sum([SUM])", "Count([agent])"
        ]

    def test_a_new_sheet_name_creates_one(self):
        engine = RecordingEngine()
        chat_tools.create_chart(
            engine, "piechart", "P", dimensions=["Region"],
            measures=["Sum([SUM])"], sheet_title="My sheet",
        )
        assert engine.sheets == ["My sheet"]

    def test_an_existing_sheet_name_adds_to_it(self):
        """The regression: "add it to the Full Dashboard sheet" made a second
        sheet with the same name, so the chart was nowhere the person looked."""
        engine = RecordingEngine(existing_sheets=["Full Dashboard"])

        chat_tools.create_chart(
            engine, "qlik-sankey-chart-ext", "Flow",
            dimensions=["Currency", "Region"], measures=["Sum([SUM])"],
            sheet_title="Full Dashboard",
        )

        assert engine.opened == ["Full Dashboard"]
        assert engine.sheets == [], "created a duplicate sheet"

    def test_an_ambiguous_name_does_not_create_yet_another(self):
        """Two sheets share a name, so open_sheet refuses. Treating that as
        "not there" and creating a third made it worse on every attempt."""
        class Ambiguous(RecordingEngine):
            def open_sheet(self, name_or_id):
                raise QlikEngineError(f"2 sheets are called {name_or_id!r}")

        engine = Ambiguous()
        with pytest.raises(QlikEngineError, match="2 sheets are called"):
            chat_tools.create_chart(
                engine, "piechart", "P", dimensions=["Region"],
                measures=["Sum([SUM])"], sheet_title="Full Dashboard",
            )
        assert engine.sheets == []

    def test_it_starts_a_sheet_when_there_is_none(self):
        """Otherwise the first chart of a session has nowhere to go."""
        engine = RecordingEngine()
        chat_tools.create_chart(
            engine, "piechart", "P", dimensions=["Region"], measures=["Sum([SUM])"]
        )
        assert engine.sheets == ["P"]

    def test_it_adds_to_the_current_sheet_otherwise(self):
        engine = RecordingEngine()
        engine.sheet_handle = 1
        chat_tools.create_chart(
            engine, "piechart", "P", dimensions=["Region"], measures=["Sum([SUM])"]
        )
        assert engine.sheets == []

    def test_it_saves(self):
        engine = RecordingEngine()
        engine.sheet_handle = 1
        chat_tools.create_chart(engine, "kpi", "Total", measures=["Sum([SUM])"])
        assert engine.saved == 1

    def test_colour_and_limit_reach_the_engine(self):
        engine = RecordingEngine()
        engine.sheet_handle = 1
        chat_tools.create_chart(
            engine, "barchart", "B", dimensions=["Region"],
            measures=["Sum([SUM])"], color="red", limit=5,
        )
        _, _, kwargs = engine.charts[0]
        assert kwargs["colour"] == "red"
        assert kwargs["limit"] == 5


class TestAdviceIsFollowable:

    def test_the_rejection_message_names_a_tool_the_assistant_has(self):
        """It used to say "use qlik_build_sheet", which does not exist here."""
        from dashboard_builder import normalize_visualization, validate_visualization

        viz = normalize_visualization({
            "type": "scatterplot", "title": "t", "dimension": "Region",
            "measure_expression": "Sum([SUM])",
        })
        error = validate_visualization(viz, {"Region", "SUM"})

        assert "create_chart" in error
        assert "qlik_build_sheet" not in error

    def test_the_prompt_says_which_tool_to_use(self):
        prompt = chat_tools.SYSTEM_PROMPT
        assert "must be built with" in prompt
        assert "create_chart" in prompt

    def test_the_tool_description_lists_what_needs_it(self):
        tool = next(
            t for t in chat_tools.TOOLS if t["function"]["name"] == "create_chart"
        )
        description = tool["function"]["description"]
        for word in ("sankey", "scatter", "mekko"):
            assert word in description


class TestSheetAttribution:
    """"Which sheet is that chart on?" had no answer: a chart's properties
    say nothing about where it lives, only the sheet's `cells` list does."""

    def test_list_charts_reports_the_sheet(self, engine):
        engine.ws.handlers = {
            "CreateSessionObject": {"qReturn": {"qHandle": 7, "qGenericId": "OBJ"}},
            "GetLayout": {"qLayout": {"qAppObjectList": {"qItems": [
                {"qInfo": {"qId": "SH_1"}, "qData": {"title": "Full Dashboard", "cells": [{}]}},
            ]}}},
            "DestroySessionObject": {},
            "GetAllInfos": {"qInfos": [{"qId": "PIE_1", "qType": "piechart"}]},
            "GetObject": {"qReturn": {"qHandle": 9}},
            "GetProperties": {"qProp": {
                "qInfo": {"qId": "PIE_1", "qType": "piechart"},
                "title": "A pie",
                "cells": [{"name": "PIE_1"}],
                "qHyperCubeDef": {"qDimensions": [], "qMeasures": []},
            }},
        }

        charts = engine.list_charts()

        assert charts[0]["sheet"] == "Full Dashboard"
        assert charts[0]["sheet_id"] == "SH_1"

    def test_a_chart_on_no_sheet_is_reported_as_such(self, engine):
        """An object can exist in the app while being invisible in Qlik."""
        engine.ws.handlers = {
            "CreateSessionObject": {"qReturn": {"qHandle": 7, "qGenericId": "OBJ"}},
            "GetLayout": {"qLayout": {"qAppObjectList": {"qItems": []}}},
            "DestroySessionObject": {},
            "GetAllInfos": {"qInfos": [{"qId": "PIE_1", "qType": "piechart"}]},
            "GetObject": {"qReturn": {"qHandle": 9}},
            "GetProperties": {"qProp": {
                "qInfo": {"qId": "PIE_1", "qType": "piechart"},
                "title": "Orphan",
                "qHyperCubeDef": {"qDimensions": [], "qMeasures": []},
            }},
        }

        assert engine.list_charts()[0]["sheet"] is None
