"""The assistant must not be told things that aren't true.

A rule added to stop it inventing scatter plots said "the only chart types
that exist are kpi, barchart, linechart, piechart and table". When 18 more
types were added the rule stayed, so the assistant kept refusing to draw a
bullet chart - correctly, according to what it had been told. Hand-maintained
lists of generated data go stale; these tests make that fail loudly.
"""

import pytest

from chart_specs import CHART_TYPES, describe_chart_types
from chat_tools import SYSTEM_PROMPT, TOOLS


class TestCatalogueIsGenerated:

    def test_every_type_appears(self):
        catalogue = describe_chart_types()
        for chart_type in CHART_TYPES:
            assert chart_type in catalogue, f"{chart_type} missing from the catalogue"

    def test_it_states_the_requirements(self):
        """Knowing a scatter plot exists is useless without knowing it needs
        two measures."""
        catalogue = describe_chart_types()
        assert "scatterplot (1 dimension, 2-3 measures)" in catalogue
        assert "gauge (no dimensions, 1 measure)" in catalogue

    def test_open_ended_maximums_read_sensibly(self):
        assert "0+ dimensions" in describe_chart_types()


class TestSystemPromptTellsTheTruth:

    def test_it_no_longer_denies_the_new_types(self):
        assert "there is no scatter plot" not in SYSTEM_PROMPT
        assert "only chart types that exist are kpi" not in SYSTEM_PROMPT

    @pytest.mark.parametrize("chart_type", [
        "bulletchart", "scatterplot", "treemap", "gauge", "combochart",
        "waterfallchart", "boxplot", "histogram",
    ])
    def test_the_new_types_are_named(self, chart_type):
        assert chart_type in SYSTEM_PROMPT

    def test_it_says_they_can_be_built(self):
        """It refused a bullet chart outright rather than trying."""
        assert "you CAN build it" in SYSTEM_PROMPT

    def test_it_still_forbids_inventing_types(self):
        """The original rule was right, just wrong about the list."""
        assert "do not invent one that is not there" in SYSTEM_PROMPT

    def test_everyday_names_are_offered(self):
        for word in ("scatter", "pivot table", "combo", "funnel"):
            assert word in SYSTEM_PROMPT


class TestNoHandMaintainedTypeLists:
    """Any place that spells the types out by hand will drift again."""

    STALE = "kpi, barchart, linechart, piechart, table"

    def test_the_prompt_has_no_frozen_list(self):
        assert self.STALE not in SYSTEM_PROMPT

    def test_no_tool_description_has_a_frozen_list(self):
        for tool in TOOLS:
            description = tool["function"]["description"]
            assert self.STALE not in description, tool["function"]["name"]

    def test_the_build_tool_points_at_the_catalogue(self):
        build = next(t for t in TOOLS if t["function"]["name"] == "build_dashboard")
        assert "AVAILABLE CHART TYPES" in build["function"]["description"]
