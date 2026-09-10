import importlib
import sys
import types

import pytest

LEGACY_TABLE = {
    "bundle": "table",
    "dimensions": (0, 1000),
    "measures": (0, 1000),
    "properties": {
        "visualization": "table",
        "title": "",
        "subtitle": "",
        "footnote": "",
        "totals": {"show": True, "position": "noTotals"},
        "qHyperCubeDef": {
            "qDimensions": [],
            "qMeasures": [],
            "qColumnOrder": [],
            "qInitialDataFetch": [{"qTop": 0, "qLeft": 0,
                                   "qHeight": 20, "qWidth": 20}],
        },
    },
}

LEGACY_PIVOT = {
    "bundle": "pivot-table",
    "dimensions": (2, 1000),
    "measures": (1, 1000),
    "properties": {
        "visualization": "pivot-table",
        "title": "",
        "subtitle": "",
        "footnote": "",
        "qHyperCubeDef": {"qDimensions": [], "qMeasures": [], "qMode": "P",
                          "qNoOfLeftDims": 1},
    },
}


from conftest import NEUTRAL_OVERRIDES


def load(overrides=None, available=()):
    sys.modules.pop("chart_specs", None)

    if overrides is None:
        sys.modules["chart_overrides"] = NEUTRAL_OVERRIDES
    else:
        module = types.ModuleType("chart_overrides")
        module.CHART_OVERRIDES = overrides
        module.AVAILABLE_TYPES = tuple(available)
        sys.modules["chart_overrides"] = module

    return importlib.import_module("chart_specs")


@pytest.fixture(autouse=True)
def restore():
    yield
    sys.modules["chart_overrides"] = NEUTRAL_OVERRIDES
    sys.modules.pop("chart_specs", None)
    importlib.import_module("chart_specs")


OLD_SERVER = ("barchart", "linechart", "piechart", "kpi", "table", "pivot-table")


class TestAServerWithoutTheNebulaBundles:
    def test_a_table_request_becomes_the_type_the_server_has(self):
        specs = load({"table": LEGACY_TABLE}, OLD_SERVER)

        for asked in ("table", "straight table", "datatable", "data table"):
            assert specs.resolve_chart_type(asked) == "table", asked

    def test_a_pivot_request_becomes_the_type_the_server_has(self):
        specs = load({"pivot-table": LEGACY_PIVOT}, OLD_SERVER)

        for asked in ("pivot", "pivot table", "pivottable"):
            assert specs.resolve_chart_type(asked) == "pivot-table", asked

    def test_the_grid_chart_lands_on_a_pivot_the_server_can_draw(self):
        specs = load({"pivot-table": LEGACY_PIVOT}, OLD_SERVER)

        assert specs.resolve_chart_type("grid") == "pivot-table"

    def test_native_types_are_untouched(self):
        specs = load({"table": LEGACY_TABLE}, OLD_SERVER)

        for asked in ("barchart", "linechart", "piechart", "kpi"):
            assert specs.resolve_chart_type(asked) == asked

    def test_the_harvested_tree_is_what_gets_written(self):
        specs = load({"table": LEGACY_TABLE}, OLD_SERVER)

        cube = {"qDimensions": [{"qDef": {"qFieldDefs": ["BANK"]}}],
                "qMeasures": []}
        properties = specs.build_properties("table", "OBJ_1", "Accounts", cube)

        assert properties["qInfo"]["qType"] == "table"
        assert properties["visualization"] == "table"
        assert properties["totals"] == {"show": True, "position": "noTotals"}
        assert properties["qHyperCubeDef"]["qInitialDataFetch"] == [
            {"qTop": 0, "qLeft": 0, "qHeight": 20, "qWidth": 20}
        ]
        assert properties["qHyperCubeDef"]["qDimensions"] == cube["qDimensions"]

    def test_column_order_is_still_filled_in_for_the_legacy_tree(self):
        specs = load({"table": LEGACY_TABLE}, OLD_SERVER)

        cube = {
            "qDimensions": [{"qDef": {"qFieldDefs": ["BANK"]}},
                            {"qDef": {"qFieldDefs": ["BRANCH"]}}],
            "qMeasures": [{"qDef": {"qDef": "=Count(ACCOUNT)"}}],
        }
        properties = specs.build_properties("table", "OBJ_2", "T", cube)

        assert properties["qHyperCubeDef"]["qColumnOrder"] == [0, 1, 2]

    def test_a_fallback_never_offers_a_type_the_server_lacks(self):
        specs = load({"table": LEGACY_TABLE}, OLD_SERVER)

        for source in ("barchart", "linechart", "piechart"):
            for candidate in specs.fallback_types(source, ["BANK"], ["=Sum(X)"]):
                assert candidate in OLD_SERVER, (source, candidate)

    def test_a_fallback_does_not_offer_the_type_that_just_failed(self):
        specs = load({"table": LEGACY_TABLE}, OLD_SERVER)

        assert "table" not in specs.fallback_types("table", ["BANK"], [])

    def test_chart_requirements_come_from_the_harvested_entry(self):
        specs = load({"pivot-table": LEGACY_PIVOT}, OLD_SERVER)

        (dmin, dmax), (mmin, mmax) = specs.chart_requirements("pivot-table")
        assert (dmin, dmax) == (2, 1000)
        assert (mmin, mmax) == (1, 1000)


class TestAServerThatHasEverything:
    def test_nothing_changes_when_no_overrides_are_installed(self):
        specs = load(None)

        assert specs.resolve_chart_type("table") == "sn-table"
        assert specs.resolve_chart_type("pivot") == "sn-pivot-table"
        assert specs.AVAILABLE_TYPES == ()

    def test_an_empty_available_list_means_do_not_restrict(self):
        specs = load({}, ())

        assert specs.resolve_chart_type("table") == "sn-table"

    def test_a_modern_server_keeps_the_nebula_table(self):
        specs = load({}, ("sn-table", "sn-pivot-table", "barchart"))

        assert specs.resolve_chart_type("table") == "sn-table"
        assert specs.resolve_chart_type("pivot") == "sn-pivot-table"


class TestTheOverrideCannotSilentlyBreakThings:
    def test_an_unknown_available_type_falls_back_to_the_original(self):
        specs = load({}, ("barchart",))

        assert specs.resolve_chart_type("table") == "sn-table"

    def test_an_override_without_a_matching_available_entry_is_ignored(self):
        specs = load({"table": LEGACY_TABLE}, ("sn-table", "barchart"))

        assert specs.resolve_chart_type("table") == "sn-table"

    def test_every_override_is_reachable_through_chart_types(self):
        specs = load({"table": LEGACY_TABLE, "pivot-table": LEGACY_PIVOT},
                     OLD_SERVER)

        assert "table" in specs.CHART_TYPES
        assert "pivot-table" in specs.CHART_TYPES


class TestTheHarvesterIgnoresOurOwnObjects:
    def test_our_object_ids_are_recognised(self):
        from harvest_charts import OURS

        for object_id in ("SN-_a9cc9b4b", "BAR_1234abcd", "KPI_deadbeef",
                          "SH_6bbf48b9", "TAB_00112233", "PIE_0a1b2c3d"):
            assert OURS.match(object_id), object_id

    def test_qlik_own_ids_are_not_mistaken_for_ours(self):
        from harvest_charts import OURS

        for object_id in ("xYVDna", "vmBbSj", "nLShfmC", "Ppkugz", "aBcDeF",
                          "db4bb850-c9ee-4859-95df-0bcef7cffee5"):
            assert not OURS.match(object_id), object_id

    def test_an_object_we_built_must_not_prove_a_type_is_installed(self):
        from harvest_charts import OURS

        assert OURS.match("SN-_a9cc9b4b"), (
            "an unrenderable sn-table this project created still sits on the "
            "sheet - counting it would make the harvester declare sn-table "
            "available and defeat the whole fix"
        )


class TestRefusingNeedsPositiveEvidence:
    HARVESTED = ("pivot-table", "table")

    def test_only_types_with_a_confirmed_sibling_are_refused(self):
        specs = load({"table": LEGACY_TABLE, "pivot-table": LEGACY_PIVOT},
                     self.HARVESTED)

        assert not specs.available("sn-table")
        assert not specs.available("sn-pivot-table")

    def test_a_type_nobody_built_by_hand_is_still_allowed(self):
        specs = load({"table": LEGACY_TABLE, "pivot-table": LEGACY_PIVOT},
                     self.HARVESTED)

        for chart_type in ("barchart", "linechart", "piechart", "kpi",
                           "treemap", "scatterplot", "combochart", "gauge"):
            assert specs.available(chart_type), (
                f"{chart_type} was refused because nobody happened to build one "
                f"by hand - a harvest of two table types must not disable every "
                f"other chart"
            )

    def test_a_bar_chart_still_resolves_and_builds(self):
        specs = load({"table": LEGACY_TABLE}, self.HARVESTED)

        assert specs.resolve_chart_type("barchart") == "barchart"
        properties = specs.build_properties(
            "barchart", "BAR_1", "Accounts by branch",
            {"qDimensions": [{"qDef": {"qFieldDefs": ["BRANCH"]}}],
             "qMeasures": [{"qDef": {"qDef": "=Count(ACCOUNT)"}}]})
        assert properties["qInfo"]["qType"] == "barchart"


ONE_COLUMN_SAMPLE = {
    "bundle": "table",
    "dimensions": (0, 1000),
    "measures": (0, 1000),
    "properties": {
        "visualization": "table",
        "title": "", "subtitle": "", "footnote": "",
        "qHyperCubeDef": {
            "qDimensions": [], "qMeasures": [],
            "qColumnOrder": [],
            "columnOrder": [0],
            "columnWidths": [-1],
            "qInterColumnSortOrder": [0],
        },
    },
}


class TestASampleWithFewColumnsDoesNotShrinkTheChart:
    def cube(self, count):
        dims = [{"qDef": {"qFieldDefs": [f"F{i}"]}} for i in range(count - 1)]
        return {"qDimensions": dims,
                "qMeasures": [{"qDef": {"qDef": "=Count(X)"}}]}

    def test_every_column_list_matches_the_column_count(self):
        specs = load({"table": ONE_COLUMN_SAMPLE}, ("table",))

        for count in (1, 2, 4, 10):
            properties = specs.build_properties(
                "table", "T", "Wide", self.cube(count))
            hypercube = properties["qHyperCubeDef"]

            for key in ("qColumnOrder", "columnOrder", "qInterColumnSortOrder"):
                assert hypercube[key] == list(range(count)), (key, count)
            assert hypercube["columnWidths"] == [-1] * count, count

    def test_a_stale_order_inside_the_cube_is_corrected(self):
        specs = load({"table": ONE_COLUMN_SAMPLE}, ("table",))

        properties = specs.build_properties("table", "T", "T", self.cube(3))

        assert properties["qHyperCubeDef"]["columnOrder"] != [0], (
            "the harvested sample had one column - leaving its columnOrder in "
            "place makes a three-column table render one column"
        )

    def test_the_harvester_does_not_bake_in_a_sample_column_count(self):
        from harvest_charts import clean

        harvested = clean({
            "qInfo": {"qId": "xYVDna", "qType": "table"},
            "columnOrder": [0, 1],
            "columnWidths": [-1, -1],
            "qHyperCubeDef": {
                "qDimensions": [{"a": 1}], "qMeasures": [{"b": 2}],
                "qColumnOrder": [0, 1], "columnOrder": [0, 1],
                "columnWidths": [-1, -1], "qInterColumnSortOrder": [0, 1],
            },
        })

        cube = harvested["qHyperCubeDef"]
        for key in ("qColumnOrder", "columnOrder", "columnWidths",
                    "qInterColumnSortOrder"):
            assert cube[key] == [], key
        assert harvested["columnOrder"] == []
        assert harvested["columnWidths"] == []
