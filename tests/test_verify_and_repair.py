import pytest

from chart_specs import fallback_types
from qlik_engine import QlikEngine


def layout(size=None, pages=None, key="qDataPages", error=None, branch=None):
    hypercube = {"qSize": size if size is not None else {"qcx": 2, "qcy": 5}}
    if error:
        hypercube["qError"] = error
    if pages is not None:
        hypercube[key] = pages
    return {branch: {"qHyperCube": hypercube}} if branch else {"qHyperCube": hypercube}


class FakeEngine(QlikEngine):
    def __init__(self, properties, layout_result, fail=False):
        self.properties = properties
        self.layout_result = layout_result
        self.fail = fail
        self.destroyed = []

    def _object_handle(self, object_id):
        if self.fail:
            raise QlikEngineError("no such object")
        return 1

    def send(self, method, handle=None, params=None):
        if method == "GetProperties":
            return {"result": {"qProp": self.properties}}
        if method == "GetLayout":
            return {"result": {"qLayout": self.layout_result}}
        raise AssertionError(f"unexpected call {method}")


from qlik_engine import QlikEngineError

TOP = {"qHyperCubeDef": {}}
NESTED = {"boxplotDef": {"qHyperCubeDef": {}}}


class TestItCatchesTheFailuresThatWereRealBugs:

    def test_rows_promised_but_no_page_delivers_them(self):
        engine = FakeEngine(TOP, layout(size={"qcx": 1, "qcy": 40}, pages=[]))
        ok, detail = engine.chart_renders("OBJ")
        assert ok is False
        assert "40 rows" in detail and "no data" in detail

    def test_a_cube_with_no_rows_at_all(self):
        engine = FakeEngine(TOP, layout(size={"qcx": 0, "qcy": 0}))
        ok, detail = engine.chart_renders("OBJ")
        assert ok is False
        assert "no rows" in detail

    def test_an_engine_error_on_the_object(self):
        engine = FakeEngine(TOP, layout(error={"qErrorCode": 7}))
        ok, detail = engine.chart_renders("OBJ")
        assert ok is False
        assert "error" in detail

    def test_a_nested_cube_is_read_from_its_own_branch(self):
        engine = FakeEngine(
            NESTED,
            layout(pages=[{"qMatrix": [[1, 2]]}], branch="boxplotDef"),
        )
        assert engine.chart_renders("OBJ")[0] is True


class TestItDoesNotCryWolf:
    @pytest.mark.parametrize("key", [
        "qDataPages", "qPivotDataPages", "qStackedDataPages", "qTreeDataPages",
    ])
    def test_every_page_kind_counts_as_data(self, key):
        engine = FakeEngine(TOP, layout(pages=[{"qData": [[1]]}], key=key))
        assert engine.chart_renders("OBJ")[0] is True

    def test_a_chart_that_cannot_be_read_is_left_alone(self):
        engine = FakeEngine(TOP, layout(), fail=True)
        ok, detail = engine.chart_renders("OBJ")
        assert ok is True
        assert "assumed fine" in detail

    def test_a_component_with_no_cube_in_its_layout_is_left_alone(self):
        engine = FakeEngine(TOP, {})
        ok, detail = engine.chart_renders("OBJ")
        assert ok is True
        assert "assumed fine" in detail

    def test_qnodes_counts_as_data(self):
        engine = FakeEngine(TOP, layout(pages=[{"qNodes": [{"a": 1}]}]))
        assert engine.chart_renders("OBJ")[0] is True


class TestTheReplacementCanActuallyBeBuilt:
    def test_a_box_plot_falls_back_to_a_bar_chart(self):
        assert fallback_types("boxplot", ["Region"], ["Sum([S])"])[0] == "barchart"

    def test_a_grid_chart_falls_back_to_a_pivot(self):
        assert fallback_types(
            "sn-grid-chart", ["Region", "agent"], ["Sum([S])"]
        )[0] == "sn-pivot-table"

    def test_a_fallback_that_cannot_take_the_dimensions_is_not_offered(self):
        offered = fallback_types(
            "boxplot", ["A", "B", "C", "D"], ["Sum([S])"]
        )
        assert "barchart" not in offered

    def test_there_is_always_something_left(self):
        assert fallback_types("sn-grid-chart", ["A", "B"], ["Sum([S])"])[-1] == "sn-table"

    def test_a_two_measure_chart_keeps_both(self):
        offered = fallback_types("scatterplot", ["Region"], ["Sum([A])", "Sum([B])"])
        assert "combochart" in offered

    def test_an_unknown_type_still_gets_a_fallback(self):
        assert fallback_types("nonsense", ["Region"], ["Sum([S])"]) == ["sn-table"]
