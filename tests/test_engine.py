"""Engine protocol handling, against a fake websocket."""

import pytest

from qlik_engine import (
    QlikEngine,
    QlikEngineError,
    QlikNotConnectedError,
)

from conftest import FakeEngineSocket

FIELD_LIST_LAYOUT = {
    "qLayout": {
        "qFieldList": {
            "qItems": [
                {"qName": "Sales", "qTags": ["$numeric"], "qSrcTables": ["Orders"]},
                {"qName": "Region", "qTags": ["$ascii", "$text"], "qSrcTables": ["Orders"]},
                {"qName": "Order Id", "qTags": ["$key"], "qSrcTables": ["Orders"]},
            ]
        }
    }
}

SHEET_LIST_LAYOUT = {
    "qLayout": {
        "qAppObjectList": {
            "qItems": [
                {
                    "qInfo": {"qId": "SH_abc"},
                    "qData": {"title": "Sales", "description": "d", "cells": [{}, {}]},
                },
                {"qInfo": {"qId": "SH_def"}, "qData": {"title": "Empty", "cells": None}},
            ]
        }
    }
}


class TestSend:

    def test_returns_the_matching_response(self, engine):
        engine.ws.handlers = {"GetAllInfos": {"qInfos": [{"qId": "a", "qType": "kpi"}]}}
        response = engine.send("GetAllInfos", handle=1)
        assert response["result"]["qInfos"][0]["qId"] == "a"

    def test_increments_the_request_id(self, engine):
        engine.ws.handlers = {"Noop": {}}
        engine.send("Noop")
        engine.send("Noop")
        assert [r["id"] for r in engine.ws.sent] == [1, 2]

    def test_skips_unsolicited_notifications(self, engine):
        """The engine interleaves OnConnected and change events with replies."""
        engine.ws = FakeEngineSocket(handlers={"Noop": {"ok": True}}, noisy=True)
        assert engine.send("Noop")["result"] == {"ok": True}

    def test_skips_a_straggler_from_an_earlier_call(self, engine):
        """A reply to a call that already timed out must not be mistaken for
        the answer to this one."""
        engine.ws.handlers = {"Noop": {"ok": True}}
        engine.ws.push_unsolicited({"jsonrpc": "2.0", "id": 999, "result": {"stale": True}})
        assert engine.send("Noop")["result"] == {"ok": True}

    def test_raises_on_an_engine_error(self, engine):
        engine.ws.handlers = {"OpenDoc": {"error": {"code": 1003, "message": "App not found"}}}
        with pytest.raises(QlikEngineError, match="App not found"):
            engine.send("OpenDoc")

    def test_raises_when_not_connected(self, offline_engine):
        with pytest.raises(QlikNotConnectedError):
            offline_engine.send("GetAllInfos")


class TestSessionObjects:
    """Regression: DestroySessionObject takes the object's generic id, not
    its handle. Passing the handle failed every time, and the failure was
    swallowed, so session objects accumulated for the life of the connection.
    """

    def test_destroys_using_the_generic_id(self, engine):
        engine.ws.handlers = {
            "CreateSessionObject": {"qReturn": {"qHandle": 7, "qGenericId": "OBJ_1"}},
            "GetLayout": FIELD_LIST_LAYOUT,
            "DestroySessionObject": {"qSuccess": True},
        }

        engine.get_fields()

        destroys = engine.ws.requests_for("DestroySessionObject")
        assert len(destroys) == 1
        assert destroys[0]["params"] == ["OBJ_1"]

    def test_destroys_even_when_the_body_raises(self, engine):
        engine.ws.handlers = {
            "CreateSessionObject": {"qReturn": {"qHandle": 7, "qGenericId": "OBJ_1"}},
            "GetLayout": {"error": {"message": "boom"}},
            "DestroySessionObject": {"qSuccess": True},
        }

        with pytest.raises(QlikEngineError):
            engine.get_fields()

        assert len(engine.ws.requests_for("DestroySessionObject")) == 1

    def test_a_failed_destroy_is_not_fatal(self, engine):
        engine.ws.handlers = {
            "CreateSessionObject": {"qReturn": {"qHandle": 7, "qGenericId": "OBJ_1"}},
            "GetLayout": FIELD_LIST_LAYOUT,
            "DestroySessionObject": {"error": {"message": "already gone"}},
        }
        assert len(engine.get_fields()) == 3


class TestGetFields:

    def test_parses_field_metadata(self, engine):
        engine.ws.handlers = {
            "CreateSessionObject": {"qReturn": {"qHandle": 7, "qGenericId": "OBJ_1"}},
            "GetLayout": FIELD_LIST_LAYOUT,
            "DestroySessionObject": {},
        }

        fields = engine.get_fields()

        assert [f["name"] for f in fields] == ["Sales", "Region", "Order Id"]
        assert fields[0]["is_numeric"] is True
        assert fields[2]["is_key"] is True
        assert fields[1]["is_numeric"] is False

    def test_requires_an_open_app(self, offline_engine):
        offline_engine.ws = FakeEngineSocket()
        with pytest.raises(QlikNotConnectedError, match="No app is open"):
            offline_engine.get_fields()


class TestListSheets:

    def test_reports_chart_counts(self, engine):
        engine.ws.handlers = {
            "CreateSessionObject": {"qReturn": {"qHandle": 7, "qGenericId": "OBJ_1"}},
            "GetLayout": SHEET_LIST_LAYOUT,
            "DestroySessionObject": {},
        }

        sheets = engine.list_sheets()

        assert sheets[0] == {
            "qId": "SH_abc", "title": "Sales", "description": "d", "chart_count": 2,
        }
        assert sheets[1]["chart_count"] == 0  # cells: null


class TestAppResolution:
    """Desktop opens apps by name; Enterprise needs the GUID. Resolving the
    name to an id up front is what lets one APP_NAME work in both."""

    APPS = [
        {"id": "c:/apps/Sales.qvf", "name": "Sales Dashboard", "path": "Sales.qvf"},
        {"id": "9f3c-guid", "name": "Finance", "path": "Finance"},
    ]

    @pytest.mark.parametrize("given,expected", [
        ("Sales Dashboard", "c:/apps/Sales.qvf"),   # exact title
        ("Sales.qvf", "c:/apps/Sales.qvf"),         # filename
        ("Sales", "c:/apps/Sales.qvf"),             # filename without .qvf
        ("sales dashboard", "c:/apps/Sales.qvf"),   # case-insensitive
        ("c:/apps/Sales.qvf", "c:/apps/Sales.qvf"), # already an id
        ("9f3c-guid", "9f3c-guid"),                 # enterprise guid
        ("Finance", "9f3c-guid"),
    ])
    def test_resolves_names_to_ids(self, offline_engine, given, expected):
        assert offline_engine.resolve_app_id(given, apps=self.APPS) == expected

    @pytest.mark.parametrize("given", ["Nope", "", None])
    def test_returns_none_for_unknown_names(self, offline_engine, given):
        assert offline_engine.resolve_app_id(given, apps=self.APPS) is None

    def test_open_app_sends_the_resolved_id(self, engine):
        engine.ws.handlers = {
            "GetDocList": {"qDocList": [
                {"qDocId": "9f3c-guid", "qTitle": "Finance", "qDocName": "Finance.qvf"},
            ]},
            "OpenDoc": {"qReturn": {"qHandle": 42}},
        }

        engine.open_app("Finance")

        assert engine.ws.requests_for("OpenDoc")[0]["params"] == ["9f3c-guid"]
        assert engine.app_handle == 42

    def test_falls_back_to_the_raw_name_when_listing_fails(self, engine):
        engine.ws.handlers = {
            "GetDocList": {"error": {"message": "not supported"}},
            "OpenDoc": {"qReturn": {"qHandle": 42}},
        }

        engine.open_app("data")

        assert engine.ws.requests_for("OpenDoc")[0]["params"] == ["data"]

    def test_already_open_says_what_to_do(self, engine):
        """Desktop allows one session per app and its own UI holds one, so
        this is the error you hit most and the least self-explanatory."""
        engine.ws.handlers = {
            "GetDocList": {"qDocList": [{"qDocId": "1", "qTitle": "data1"}]},
            "OpenDoc": {"error": {"message": "App already open"}},
        }

        with pytest.raises(QlikEngineError) as excinfo:
            engine.open_app("data1")

        message = str(excinfo.value)
        assert "already open" in message
        assert "Close it in the Qlik Sense window" in message

    def test_open_app_error_lists_what_is_available(self, engine):
        engine.ws.handlers = {
            "GetDocList": {"qDocList": [{"qDocId": "1", "qTitle": "Finance"}]},
            "OpenDoc": {"error": {"message": "App not found"}},
        }

        with pytest.raises(QlikEngineError, match="Finance"):
            engine.open_app("Sales")

    def test_opening_an_app_clears_the_previous_sheet(self, engine):
        engine.ws.handlers = {
            "GetDocList": {"qDocList": []},
            "OpenDoc": {"qReturn": {"qHandle": 42}},
        }
        engine.sheet_handle, engine.sheet_id = 9, "SH_old"

        engine.open_app("data")

        assert engine.sheet_handle is None
        assert engine.sheet_id is None


class TestCreateChart:

    @pytest.fixture
    def sheet_engine(self, engine):
        engine.sheet_handle = 5
        engine.sheet_id = "SH_test"
        engine.ws.handlers = {
            "CreateChild": {"qReturn": {"qHandle": 11}},
            "GetProperties": lambda r: {"qProp": {"cells": []}},
            "SetProperties": {},
        }
        return engine

    def created_properties(self, engine):
        return engine.ws.requests_for("CreateChild")[0]["params"][0]

    def test_builds_a_hypercube_with_dimension_and_measure(self, sheet_engine):
        sheet_engine.create_chart(
            "barchart", "Sales by region",
            dimension="Region", measure_expression="Sum([Sales])",
        )

        hypercube = self.created_properties(sheet_engine)["qHyperCubeDef"]
        assert hypercube["qDimensions"][0]["qDef"]["qFieldDefs"] == ["Region"]
        assert hypercube["qMeasures"][0]["qDef"]["qDef"] == "=Sum([Sales])"

    def test_measures_sort_before_dimensions(self, sheet_engine):
        """Without this the chart renders blank even with valid data."""
        sheet_engine.create_chart(
            "barchart", "t", dimension="Region", measure_expression="Sum([Sales])"
        )
        hypercube = self.created_properties(sheet_engine)["qHyperCubeDef"]
        assert hypercube["qInterColumnSortOrder"] == [1, 0]

    def test_falls_back_to_sum_of_the_measure(self, sheet_engine):
        sheet_engine.create_chart("kpi", "Total sales", measure="Sales")
        hypercube = self.created_properties(sheet_engine)["qHyperCubeDef"]
        assert hypercube["qMeasures"][0]["qDef"]["qDef"] == "=Sum([Sales])"

    def test_bracketed_names_do_not_double_up(self, sheet_engine):
        sheet_engine.create_chart(
            "barchart", "t", dimension="[Customer Segment]", measure="[Sales]"
        )
        properties = self.created_properties(sheet_engine)
        hypercube = properties["qHyperCubeDef"]
        assert hypercube["qDimensions"][0]["qDef"]["qFieldDefs"] == ["Customer Segment"]
        assert hypercube["qMeasures"][0]["qDef"]["qDef"] == "=Sum([Sales])"

    def test_does_not_double_the_leading_equals(self, sheet_engine):
        sheet_engine.create_chart("kpi", "t", measure_expression="=Sum([Sales])")
        hypercube = self.created_properties(sheet_engine)["qHyperCubeDef"]
        assert hypercube["qMeasures"][0]["qDef"]["qDef"] == "=Sum([Sales])"

    def test_registers_the_object_in_the_sheet_cells(self, sheet_engine):
        sheet_engine.create_chart("kpi", "Total", measure="Sales")

        cells = sheet_engine.ws.requests_for("SetProperties")[0]["params"][0]["cells"]
        assert len(cells) == 1
        assert cells[0]["type"] == "kpi"
        assert cells[0]["colspan"] == 6  # the kpi default from chart_specs
        assert "bounds" in cells[0]

    def test_rejects_an_unknown_chart_type(self, sheet_engine):
        with pytest.raises(QlikEngineError, match="Unknown chart type"):
            sheet_engine.create_chart("definitely-not-a-chart", "t", measure="Sales")

    def test_scatter_now_resolves_and_asks_for_what_it_needs(self, sheet_engine):
        """"scatter" used to be an unknown type. It is a real one now, and it
        needs two measures - which is why every attempt at one silently
        produced something else."""
        with pytest.raises(QlikEngineError, match="at least 2 measure"):
            sheet_engine.create_chart(
                "scatter", "t", dimension="Region", measure_expression="Sum([Sales])"
            )

    def test_rejects_a_chart_with_no_measure(self, sheet_engine):
        with pytest.raises(QlikEngineError, match="needs at least 1 measure"):
            sheet_engine.create_chart("barchart", "t", dimension="Region")

    def test_requires_an_active_sheet(self, engine):
        with pytest.raises(QlikNotConnectedError, match="No sheet is active"):
            engine.create_chart("kpi", "t", measure="Sales")


class TestLifecycle:

    def test_close_is_idempotent(self, engine):
        engine.close()
        engine.close()
        assert not engine.connected

    def test_close_clears_handles(self, engine):
        engine.sheet_handle = 5
        engine.close()
        assert engine.app_handle is None
        assert engine.sheet_handle is None

    def test_context_manager_closes(self):
        engine = QlikEngine(autoconnect=False)
        engine.ws = FakeEngineSocket()
        with engine:
            assert engine.connected
        assert not engine.connected

    def test_enterprise_mode_requires_certificates(self):
        engine = QlikEngine(
            mode="enterprise", cert_dir="", user_directory="d", user_id="u",
            autoconnect=False,
        )
        with pytest.raises(QlikEngineError, match="QLIK_CERT_DIR"):
            engine.connect()

    def test_enterprise_mode_requires_an_identity(self, tmp_path):
        for name in ("client.pem", "client_key.pem", "root.pem"):
            (tmp_path / name).write_text("x")

        engine = QlikEngine(
            mode="enterprise", cert_dir=str(tmp_path), user_directory="", user_id="",
            autoconnect=False,
        )
        with pytest.raises(QlikEngineError, match="QLIK_USER_DIRECTORY"):
            engine.connect()

    def test_enterprise_mode_reports_missing_certificate_files(self, tmp_path):
        (tmp_path / "client.pem").write_text("x")

        engine = QlikEngine(
            mode="enterprise", cert_dir=str(tmp_path), user_directory="d", user_id="u",
            autoconnect=False,
        )
        with pytest.raises(QlikEngineError, match="client_key.pem"):
            engine.connect()
