"""Reading real data: profiling, evaluation and ad-hoc queries.

These are what let the model reason about the data rather than only about
field names - the gap that produced a table grouped by 65,752 order ids.
"""

import pytest

from dashboard_builder import (
    MAX_DIMENSION_CARDINALITY,
    enrich_fields,
    normalize_visualization,
    validate_visualization,
)
from qlik_engine import QlikEngineError

FIELDS = {"Sales", "Region", "Order Id"}

SESSION = {"qReturn": {"qHandle": 7, "qGenericId": "OBJ_1"}}


def list_object_layout(cardinality, samples, tags=None, qmin=None, qmax=None):
    info = {"qCardinal": cardinality, "qTags": tags or []}
    if qmin is not None:
        info["qMin"], info["qMax"] = qmin, qmax
    return {
        "qLayout": {
            "qListObject": {
                "qDimensionInfo": info,
                "qDataPages": [{"qMatrix": [[{"qText": s}] for s in samples]}],
            }
        }
    }


def hypercube_layout(dimensions, measures, matrix, total=None):
    return {
        "qLayout": {
            "qHyperCube": {
                "qDimensionInfo": [{"qFallbackTitle": d} for d in dimensions],
                "qMeasureInfo": [{"qFallbackTitle": m} for m in measures],
                "qDataPages": [{"qMatrix": matrix}],
                "qSize": {"qcy": total if total is not None else len(matrix)},
            }
        }
    }


class TestFieldCardinality:

    def test_get_fields_keeps_the_distinct_count(self, engine):
        """qCardinal comes back from the engine for free and used to be
        discarded, leaving the model unable to tell a category from an id."""
        engine.ws.handlers = {
            "CreateSessionObject": SESSION,
            "DestroySessionObject": {},
            "GetLayout": {"qLayout": {"qFieldList": {"qItems": [
                {"qName": "Market", "qCardinal": 5, "qTags": ["$text"]},
                {"qName": "Order Id", "qCardinal": 65752, "qTags": ["$key"]},
            ]}}},
        }

        fields = {f["name"]: f for f in engine.get_fields()}
        assert fields["Market"]["cardinality"] == 5
        assert fields["Order Id"]["cardinality"] == 65752


class TestProfileField:

    def test_returns_cardinality_and_real_samples(self, engine):
        engine.ws.handlers = {
            "CreateSessionObject": SESSION,
            "DestroySessionObject": {},
            "GetLayout": list_object_layout(3, ["Consumer", "Corporate", "Home Office"]),
        }

        profile = engine.profile_field("Customer Segment")

        assert profile["cardinality"] == 3
        assert profile["samples"] == ["Consumer", "Corporate", "Home Office"]

    def test_omits_min_max_for_text_fields(self, engine):
        """The engine reports qMin/qMax as 0 for text, which reads as a real
        measurement if it isn't filtered out."""
        engine.ws.handlers = {
            "CreateSessionObject": SESSION,
            "DestroySessionObject": {},
            "GetLayout": list_object_layout(3, ["a"], tags=["$text"], qmin=0, qmax=0),
        }

        assert "min" not in engine.profile_field("Segment")

    def test_includes_min_max_for_numeric_fields(self, engine):
        engine.ws.handlers = {
            "CreateSessionObject": SESSION,
            "DestroySessionObject": {},
            "GetLayout": list_object_layout(
                193, ["1.5"], tags=["$numeric"], qmin=1.5, qmax=1999.99
            ),
        }

        profile = engine.profile_field("Sales")
        assert (profile["min"], profile["max"]) == (1.5, 1999.99)

    def test_strips_brackets_from_the_field_name(self, engine):
        engine.ws.handlers = {
            "CreateSessionObject": SESSION,
            "DestroySessionObject": {},
            "GetLayout": list_object_layout(3, ["a"]),
        }

        engine.profile_field("[Customer Segment]")

        created = engine.ws.requests_for("CreateSessionObject")[0]["params"][0]
        assert created["qListObjectDef"]["qDef"]["qFieldDefs"] == ["Customer Segment"]

    def test_profile_fields_survives_one_bad_field(self, engine):
        calls = {"n": 0}

        def layout(request):
            calls["n"] += 1
            if calls["n"] == 2:
                return {"error": {"message": "no such field"}}
            return list_object_layout(3, ["a"])

        engine.ws.handlers = {
            "CreateSessionObject": SESSION,
            "DestroySessionObject": {},
            "GetLayout": layout,
        }

        profiles = engine.profile_fields(["Good", "Bad", "AlsoGood"])
        assert len(profiles) == 3
        assert "error" in profiles[1]
        assert profiles[2]["cardinality"] == 3


class TestEvaluate:

    def test_returns_the_numeric_value(self, engine):
        engine.ws.handlers = {"EvaluateEx": {
            "qValue": {"qText": "36784735.01", "qIsNumeric": True, "qNumber": 36784735.01}
        }}

        result = engine.evaluate("Sum([Sales])")

        assert result["number"] == pytest.approx(36784735.01)
        assert result["is_numeric"] is True

    def test_strips_a_leading_equals(self, engine):
        engine.ws.handlers = {"EvaluateEx": {"qValue": {"qText": "1", "qIsNumeric": True}}}
        engine.evaluate("=Sum([Sales])")
        assert engine.ws.requests_for("EvaluateEx")[0]["params"] == ["Sum([Sales])"]

    def test_handles_a_text_result(self, engine):
        engine.ws.handlers = {"EvaluateEx": {"qValue": {"qText": "Error", "qIsNumeric": False}}}
        result = engine.evaluate("BadFunc()")
        assert result["number"] is None
        assert result["text"] == "Error"


class TestQuery:

    @pytest.fixture
    def query_engine(self, engine):
        engine.ws.handlers = {
            "CreateSessionObject": SESSION,
            "DestroySessionObject": {},
            "GetLayout": hypercube_layout(
                ["Market"], ["Sum([Sales])"],
                [
                    [{"qText": "Europe", "qNum": "NaN"}, {"qText": "10.8M", "qNum": 10872396.8}],
                    [{"qText": "LATAM", "qNum": "NaN"}, {"qText": "10.3M", "qNum": 10277612.8}],
                ],
                total=5,
            ),
        }
        return engine

    def test_returns_rows_keyed_by_column(self, query_engine):
        result = query_engine.query(dimensions=["Market"], measures=["Sum([Sales])"])

        assert result["columns"] == ["Market", "Sum([Sales])"]
        assert result["rows"][0]["Market"] == "Europe"
        assert result["rows"][0]["Sum([Sales])"] == pytest.approx(10872396.8)
        assert result["total_rows"] == 5

    def test_text_cells_use_qtext_not_the_nan_sentinel(self, query_engine):
        """Measure cells omit qIsNumeric, and text cells carry qNum as the
        string "NaN" - so the value's type is what to branch on."""
        rows = query_engine.query(dimensions=["Market"], measures=["Sum([Sales])"])["rows"]
        assert all(isinstance(r["Market"], str) for r in rows)
        assert all(isinstance(r["Sum([Sales])"], float) for r in rows)

    def test_sorts_descending_by_the_first_measure(self, query_engine):
        """How a real 'top N' is expressed - putting SortBy in the expression
        makes the chart silently fail to calculate."""
        query_engine.query(dimensions=["Market"], measures=["Sum([Sales])"], limit=5)

        cube = query_engine.ws.requests_for("CreateSessionObject")[0]["params"][0]["qHyperCubeDef"]
        assert cube["qMeasures"][0]["qSortBy"] == {"qSortByNumeric": -1}
        assert cube["qInterColumnSortOrder"][0] == 1  # the measure column
        assert cube["qInitialDataFetch"][0]["qHeight"] == 5

    def test_adds_the_equals_prefix_to_measures(self, query_engine):
        query_engine.query(measures=["Sum([Sales])"])
        cube = query_engine.ws.requests_for("CreateSessionObject")[0]["params"][0]["qHyperCubeDef"]
        assert cube["qMeasures"][0]["qDef"]["qDef"] == "=Sum([Sales])"

    def test_rejects_an_empty_query(self, engine):
        with pytest.raises(QlikEngineError, match="at least one"):
            engine.query()

    def test_clamps_the_row_limit(self, query_engine):
        query_engine.query(measures=["Sum([Sales])"], limit=99999)
        cube = query_engine.ws.requests_for("CreateSessionObject")[0]["params"][0]["qHyperCubeDef"]
        assert cube["qInitialDataFetch"][0]["qHeight"] == 1000


class TestCardinalityGuard:
    """The regression from the screenshot: a table grouped by Order Id."""

    FIELD_INFO = {
        "Order Id": {"name": "Order Id", "cardinality": 65752},
        "Market": {"name": "Market", "cardinality": 5},
        "Category Name": {"name": "Category Name", "cardinality": 50},
        "Unknown": {"name": "Unknown"},
    }

    def viz(self, dimension):
        return normalize_visualization({
            "type": "table", "title": "Sales by Order",
            "dimension": dimension, "measure": "Sales",
            "measure_expression": "Sum([Sales])",
        })

    def test_rejects_a_high_cardinality_dimension(self):
        error = validate_visualization(
            self.viz("Order Id"), {"Order Id", "Sales"}, field_info=self.FIELD_INFO
        )
        assert error is not None
        assert "65,752" in error and "identifier" in error

    @pytest.mark.parametrize("dimension", ["Market", "Category Name"])
    def test_accepts_a_real_category(self, dimension):
        error = validate_visualization(
            self.viz(dimension), {dimension, "Sales"}, field_info=self.FIELD_INFO
        )
        assert error is None

    def test_passes_when_cardinality_is_unknown(self):
        error = validate_visualization(
            self.viz("Unknown"), {"Unknown", "Sales"}, field_info=self.FIELD_INFO
        )
        assert error is None

    def test_guard_is_off_without_field_info(self):
        assert validate_visualization(self.viz("Order Id"), {"Order Id", "Sales"}) is None

    def test_threshold_is_a_sane_dimension_size(self):
        assert 20 <= MAX_DIMENSION_CARDINALITY <= 1000


class TestEnrichFields:

    def test_samples_only_plausible_dimensions(self, engine):
        engine.ws.handlers = {
            "CreateSessionObject": SESSION,
            "DestroySessionObject": {},
            "GetLayout": list_object_layout(3, ["Consumer", "Corporate"]),
        }

        fields = [
            {"name": "Market", "cardinality": 5},
            {"name": "Order Id", "cardinality": 65752},   # an id, skip
            {"name": "Constant", "cardinality": 1},       # nothing to learn
            {"name": "NoInfo"},                           # unknown, skip
        ]

        enrich_fields(engine, fields)

        assert fields[0]["samples"] == ["Consumer", "Corporate"]
        assert all("samples" not in f for f in fields[1:])

    def test_caps_how_many_fields_are_profiled(self, engine):
        engine.ws.handlers = {
            "CreateSessionObject": SESSION,
            "DestroySessionObject": {},
            "GetLayout": list_object_layout(3, ["a"]),
        }

        fields = [{"name": f"F{i}", "cardinality": 5} for i in range(50)]
        enrich_fields(engine, fields, limit=4)

        assert len(engine.ws.requests_for("CreateSessionObject")) == 4
