import pytest

from chart_specs import COLOURS, MULTI_COLOUR, build_properties, resolve_colour
from dashboard_builder import normalize_visualization


def properties(chart_type="barchart", colour=None):
    return build_properties(
        chart_type, "OBJ_1", "t", {"qDimensions": [], "qMeasures": []}, colour=colour
    )


class TestResolveColour:

    @pytest.mark.parametrize("name", ["red", "blue", "green", "orange", "purple", "teal"])
    def test_common_names_resolve(self, name):
        assert resolve_colour(name) == COLOURS[name]

    def test_names_are_case_insensitive(self):
        assert resolve_colour("RED") == resolve_colour("red")

    def test_surrounding_space_is_ignored(self):
        assert resolve_colour("  blue  ") == COLOURS["blue"]

    @pytest.mark.parametrize("value", ["multi", "colourful", "colorful", "rainbow"])
    def test_multi_colour_synonyms(self, value):
        assert resolve_colour(value) == MULTI_COLOUR

    def test_hex_is_accepted_and_normalised(self):
        assert resolve_colour("#00ff00") == "#00FF00"

    @pytest.mark.parametrize("value", ["banana", "#12345", "", None, "#xyzxyz"])
    def test_unrecognised_values_are_dropped(self, value):
        assert resolve_colour(value) is None

    def test_no_pure_primaries(self):
        assert "#FF0000" not in COLOURS.values()


class TestColourIsApplied:

    def test_auto_is_switched_off(self):
        assert properties(colour="red")["color"]["auto"] is False

    def test_the_hex_reaches_the_palette(self):
        colour = properties(colour="red")["color"]
        assert colour["mode"] == "primary"
        assert colour["paletteColor"] == {"index": -1, "color": COLOURS["red"]}

    def test_multi_colours_by_dimension(self):
        colour = properties(colour="multi")["color"]
        assert colour["mode"] == "byDimension"
        assert colour["useDimColVal"] is True

    def test_defaults_are_untouched_without_a_colour(self):
        colour = properties()["color"]
        assert colour["auto"] is True
        assert colour["paletteColor"] == {"index": 6}

    def test_expected_keys_survive_the_merge(self):
        colour = properties(colour="blue")["color"]
        for key in ("measureScheme", "dimensionScheme", "formatting", "autoMinMax"):
            assert key in colour

    @pytest.mark.parametrize("chart_type", ["barchart", "linechart", "piechart"])
    def test_applies_to_every_nebula_chart(self, chart_type):
        assert properties(chart_type, colour="green")["color"]["auto"] is False

    @pytest.mark.parametrize("chart_type", ["kpi", "table"])
    def test_types_without_a_colour_block_are_unaffected(self, chart_type):
        assert "color" not in properties(chart_type, colour="green")

    def test_an_unknown_colour_leaves_the_default(self):
        assert properties(colour="banana")["color"]["auto"] is True


class TestNestedColourPaths:
    def test_histogram_bars_take_the_colour(self):
        colour = properties("histogram", colour="red")["color"]
        assert colour["bar"]["paletteColor"] == {"index": -1, "color": COLOURS["red"]}

    def test_histogram_defaults_survive_without_a_colour(self):
        colour = properties("histogram")["color"]
        assert colour["bar"]["paletteColor"]["color"] == "#4477aa"

    def test_waterfall_rises_take_the_colour(self):
        colour = properties("waterfallchart", colour="green")["color"]
        assert colour["auto"] is False
        assert colour["positiveValue"]["paletteColor"] == {
            "index": -1, "color": COLOURS["green"],
        }

    def test_waterfall_falls_keep_their_red(self):
        colour = properties("waterfallchart", colour="green")["color"]
        assert colour["negativeValue"]["paletteColor"]["color"] == "#cc6677"

    def test_boxplot_boxes_take_the_colour(self):
        colour = properties("boxplot", colour="blue")["boxplotDef"]["color"]
        assert colour["auto"] is False
        assert colour["box"]["paletteColor"] == {"index": -1, "color": COLOURS["blue"]}

    def test_multi_keeps_the_default_on_a_single_series(self):
        colour = properties("histogram", colour="multi")["color"]
        assert colour["bar"]["paletteColor"]["color"] == "#4477aa"


class TestSpecNormalisation:

    def base(self, **extra):
        return normalize_visualization({
            "type": "barchart", "title": "t", "dimension": "Region",
            "measure_expression": "Sum([Sales])", **extra,
        })

    def test_a_named_colour_becomes_hex(self):
        assert self.base(color="Red")["color"] == COLOURS["red"]

    def test_multi_survives_normalisation(self):
        assert self.base(color="multi")["color"] == MULTI_COLOUR

    def test_a_nonsense_colour_is_dropped_not_fatal(self):
        assert self.base(color="vermilion")["color"] is None

    def test_no_colour_is_the_norm(self):
        assert self.base()["color"] is None
