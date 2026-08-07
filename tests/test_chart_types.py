"""Every chart type Qlik ships, not just the five we started with.

The property trees come from chart_defaults.py, generated from the nebula.js
bundles in the Qlik install itself. Guessing them from documentation produced
a pie chart and a table that rendered blank without erroring, three times
over - so these tests pin the things that were wrong each time.
"""

import pytest

from chart_defaults import CHART_DEFAULTS
from chart_specs import (
    CHART_TYPES,
    VERIFIED_TYPES,
    build_properties,
    chart_requirements,
    default_size,
    resolve_chart_type,
)

def hypercube():
    """A fresh one each time - dict() is a shallow copy and the inner lists
    would be shared between calls."""
    return {"qDimensions": [{"qDef": {}}], "qMeasures": [{"qDef": {}}]}

NEW_TYPES = [t for t in CHART_TYPES if t not in VERIFIED_TYPES]


class TestCoverage:

    def test_far_more_than_the_original_five(self):
        assert len(CHART_TYPES) >= 20

    def test_the_verified_five_are_still_there(self):
        for chart_type in VERIFIED_TYPES:
            assert chart_type in CHART_TYPES

    @pytest.mark.parametrize("chart_type", [
        "combochart", "scatterplot", "treemap", "gauge", "waterfallchart",
        "boxplot", "histogram", "sn-pivot-table", "filterpane", "bulletchart",
    ])
    def test_the_types_people_ask_for_exist(self, chart_type):
        assert chart_type in CHART_TYPES


class TestNameResolution:
    """The engine's names are not what anyone says out loud."""

    @pytest.mark.parametrize("said,expected", [
        ("scatter", "scatterplot"),
        ("scatter plot", "scatterplot"),
        ("pivot table", "sn-pivot-table"),
        ("pivot", "sn-pivot-table"),
        ("funnel", "qlik-funnel-chart-ext"),
        ("sankey", "qlik-sankey-chart-ext"),
        ("word cloud", "qlik-word-cloud"),
        ("combo", "combochart"),
        ("bar chart", "barchart"),
        ("waterfall", "waterfallchart"),
        ("box plot", "boxplot"),
        ("filter pane", "filterpane"),
    ])
    def test_everyday_names_resolve(self, said, expected):
        assert resolve_chart_type(said) == expected

    def test_case_and_spacing_are_ignored(self):
        assert resolve_chart_type("  Pie Chart ") == "piechart"

    def test_nonsense_resolves_to_nothing(self):
        assert resolve_chart_type("banana chart") is None

    def test_an_engine_name_passes_straight_through(self):
        assert resolve_chart_type("qlik-funnel-chart-ext") == "qlik-funnel-chart-ext"


class TestRequirements:
    """Read from each bundle's own targets. No amount of documentation
    reading would have told us a scatter plot needs two measures - which is
    why every attempt at one silently became something else."""

    def test_scatter_needs_two_measures(self):
        _, measures = chart_requirements("scatterplot")
        assert measures[0] == 2

    def test_gauge_takes_no_dimension(self):
        dimensions, measures = chart_requirements("gauge")
        assert dimensions == (0, 0)
        assert measures == (1, 1)

    def test_combo_takes_many_measures(self):
        _, measures = chart_requirements("combochart")
        assert measures[1] >= 2

    def test_mekko_needs_two_dimensions(self):
        dimensions, _ = chart_requirements("mekkochart")
        assert dimensions[0] == 2

    def test_the_original_five_kept_sane_rules(self):
        assert chart_requirements("piechart") == ((1, 1), (1, 2))
        assert chart_requirements("kpi")[0] == (0, 0)

    def test_an_unknown_type_is_unconstrained(self):
        dimensions, measures = chart_requirements("nope")
        assert dimensions[0] == 0 and measures[0] == 0


class TestGeneratedProperties:

    @pytest.mark.parametrize("chart_type", NEW_TYPES)
    def test_identifies_the_object(self, chart_type):
        properties = build_properties(chart_type, "OBJ_1", "t", hypercube())
        assert properties["qInfo"] == {"qId": "OBJ_1", "qType": chart_type}
        assert properties["visualization"] == chart_type
        assert properties["title"] == "t"

    @pytest.mark.parametrize("chart_type", NEW_TYPES)
    def test_carries_the_hypercube(self, chart_type):
        assert "qHyperCubeDef" in build_properties(chart_type, "O", "t", hypercube())

    @pytest.mark.parametrize("chart_type", NEW_TYPES)
    def test_has_a_usable_grid_size(self, chart_type):
        colspan, rowspan = default_size(chart_type)
        assert 1 <= colspan <= 24 and rowspan >= 1

    @pytest.mark.parametrize("chart_type", NEW_TYPES)
    def test_keeps_the_bundle_defaults(self, chart_type):
        """The point of generating these: the renderer's expected keys are
        present, rather than only the ones we thought to include."""
        generated = build_properties(chart_type, "O", "t", hypercube())
        for key in CHART_DEFAULTS[chart_type]["properties"]:
            if key in ("qHyperCubeDef", "qInfo", "title", "visualization"):
                continue
            assert key in generated, f"{chart_type} lost {key}"

    def test_our_dimensions_reach_the_chart(self):
        cube = {"qDimensions": [{"qDef": {"qFieldDefs": ["Region"]}}], "qMeasures": []}
        properties = build_properties("treemap", "O", "t", cube)
        assert properties["qHyperCubeDef"]["qDimensions"][0]["qDef"]["qFieldDefs"] == ["Region"]

    def test_the_bundles_own_hypercube_settings_survive(self):
        """Merged into the bundle's qHyperCubeDef, not over the top of it -
        those carry per-chart settings like the data window."""
        base = CHART_DEFAULTS["treemap"]["properties"].get("qHyperCubeDef", {})
        extra = [k for k in base if k not in ("qDimensions", "qMeasures")]
        properties = build_properties("treemap", "O", "t", hypercube())
        for key in extra:
            assert key in properties["qHyperCubeDef"]

    def test_the_generated_templates_are_not_shared_between_calls(self):
        """CHART_DEFAULTS is module-level data; a chart that mutated it would
        corrupt every chart built afterwards."""
        first = build_properties("treemap", "O", "t", hypercube())
        mutable = next(
            (k for k, v in first.items() if isinstance(v, dict) and k != "qHyperCubeDef"),
            None,
        )
        assert mutable, "expected at least one nested property to test with"
        first[mutable]["injected"] = True

        second = build_properties("treemap", "O", "t", hypercube())
        assert "injected" not in second[mutable]


class TestVerifiedTypesUnchanged:
    """The five that were confirmed rendering keep their hand-checked trees;
    the generated defaults must not quietly replace them."""

    def test_pie_still_uses_donut(self):
        properties = build_properties("piechart", "O", "t", hypercube())
        assert "donut" in properties
        assert set(properties["dataPoint"]) == {"auto", "labelMode", "labelValueMode"}

    def test_bar_still_has_its_axes(self):
        properties = build_properties("barchart", "O", "t", hypercube())
        assert properties["dimensionAxis"]["show"] == "all"
        assert properties["measureAxis"]["show"] == "all"

    def test_table_still_declares_column_order(self):
        properties = build_properties("table", "O", "t", hypercube())
        assert properties["columnOrder"] == [0, 1]
