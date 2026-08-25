"""Reading a built chart back, and swapping the type when it will not draw.

Every check that runs BEFORE a chart is created asks about the data: does the
field exist, does the expression evaluate, does the query return rows. Three
separate bugs walked past all of it by being about the OBJECT rather than the
data - a table built from a property tree the client had outgrown, a pivot
with both dimensions on one axis, a box plot whose cube went to the top level
when its component reads boxplotDef. Each time the engine computed the numbers
perfectly and the client drew an empty box.

So the chart is read back after it is built, and a type that will not draw is
replaced by one that shows the same numbers a different way. Retrying the same
type would be pointless: these are property-tree faults and they reproduce
exactly.

The verdict decides whether a chart gets DELETED, so the tests that matter
most here are the ones about not crying wolf. An audit written the obvious way
- checking qDataPages and nothing else - called five healthy treemaps broken,
because a treemap keeps its data in qStackedDataPages. That mistake wired to a
delete would have destroyed them.
"""

import pytest

from chart_specs import fallback_types
from qlik_engine import QlikEngine


def layout(size=None, pages=None, key="qDataPages", error=None, branch=None):
    """A GetLayout result shaped the way the engine returns one."""
    hypercube = {"qSize": size if size is not None else {"qcx": 2, "qcy": 5}}
    if error:
        hypercube["qError"] = error
    if pages is not None:
        hypercube[key] = pages
    return {branch: {"qHyperCube": hypercube}} if branch else {"qHyperCube": hypercube}


class FakeEngine(QlikEngine):
    """Answers GetProperties/GetLayout from the test, touching no socket."""

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


from qlik_engine import QlikEngineError  # noqa: E402  (used by FakeEngine)

TOP = {"qHyperCubeDef": {}}
NESTED = {"boxplotDef": {"qHyperCubeDef": {}}}


class TestItCatchesTheFailuresThatWereRealBugs:

    def test_rows_promised_but_no_page_delivers_them(self):
        """The box plot's signature, and the grid chart's."""
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
        """A box plot's data is under boxplotDef; reading the top level would
        find nothing and condemn a chart that draws perfectly."""
        engine = FakeEngine(
            NESTED,
            layout(pages=[{"qMatrix": [[1, 2]]}], branch="boxplotDef"),
        )
        assert engine.chart_renders("OBJ")[0] is True


class TestItDoesNotCryWolf:
    """A wrong verdict here deletes someone's chart."""

    @pytest.mark.parametrize("key", [
        "qDataPages", "qPivotDataPages", "qStackedDataPages", "qTreeDataPages",
    ])
    def test_every_page_kind_counts_as_data(self, key):
        """The treemap mistake: a stacked cube keeps its data in
        qStackedDataPages, and checking only qDataPages called five working
        charts broken."""
        engine = FakeEngine(TOP, layout(pages=[{"qData": [[1]]}], key=key))
        assert engine.chart_renders("OBJ")[0] is True

    def test_a_chart_that_cannot_be_read_is_left_alone(self):
        engine = FakeEngine(TOP, layout(), fail=True)
        ok, detail = engine.chart_renders("OBJ")
        assert ok is True
        assert "assumed fine" in detail

    def test_a_component_with_no_cube_in_its_layout_is_left_alone(self):
        """Not every component surfaces one, and absence is not a fault."""
        engine = FakeEngine(TOP, {})
        ok, detail = engine.chart_renders("OBJ")
        assert ok is True
        assert "assumed fine" in detail

    def test_qnodes_counts_as_data(self):
        engine = FakeEngine(TOP, layout(pages=[{"qNodes": [{"a": 1}]}]))
        assert engine.chart_renders("OBJ")[0] is True


class TestTheReplacementCanActuallyBeBuilt:
    """A fallback the engine would refuse turns one broken chart into a
    confusing rejection."""

    def test_a_box_plot_falls_back_to_a_bar_chart(self):
        assert fallback_types("boxplot", ["Region"], ["Sum([S])"])[0] == "barchart"

    def test_a_grid_chart_falls_back_to_a_pivot(self):
        """Two dimensions and one measure - a pivot takes exactly that."""
        assert fallback_types(
            "sn-grid-chart", ["Region", "agent"], ["Sum([S])"]
        )[0] == "sn-pivot-table"

    def test_a_fallback_that_cannot_take_the_dimensions_is_not_offered(self):
        """A bar chart takes at most two dimensions, so it is no use to a
        three-dimension chart and must not be suggested."""
        offered = fallback_types(
            "boxplot", ["A", "B", "C", "D"], ["Sum([S])"]
        )
        assert "barchart" not in offered

    def test_there_is_always_something_left(self):
        """sn-table accepts any shape, so a chain never runs out."""
        assert fallback_types("sn-grid-chart", ["A", "B"], ["Sum([S])"])[-1] == "sn-table"

    def test_a_two_measure_chart_keeps_both(self):
        offered = fallback_types("scatterplot", ["Region"], ["Sum([A])", "Sum([B])"])
        assert "combochart" in offered

    def test_an_unknown_type_still_gets_a_fallback(self):
        assert fallback_types("nonsense", ["Region"], ["Sum([S])"]) == ["sn-table"]
