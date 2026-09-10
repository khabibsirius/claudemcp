import pytest
from fastapi.testclient import TestClient

import history
import session
import web_app
from chat_tools import DESTRUCTIVE, LOAD_EDITOR_TOOLS, execute

SCRIPT = "///$tab Main\r\nLOAD * FROM [lib://dataset/x.csv];\r\n///$tab other\r\nTRACE hi;\r\n"


class FakeEngine:
    mode = "desktop"
    connected = True

    def __init__(self, reload_ok=True):
        self.script = SCRIPT
        self.saved = False
        self.reloaded = False
        self.reload_ok = reload_ok
        self.rejected = None

    def open_app(self, name):
        self.connected = True
        self.app_name = name
        return 1

    def get_script(self):
        return self.script

    def set_script(self, script, validate=True):
        if self.rejected and self.rejected in script:
            from qlik_engine import QlikEngineError
            raise QlikEngineError("Script has 1 syntax error(s) and was not applied")
        previous, self.script = self.script, script
        return previous

    def list_connections(self):
        return [{"id": "1", "name": "dataset", "path": "C:/data/", "type": "folder"}]

    def list_sheets(self):
        return [{"qId": "SH_1", "title": "Sales", "description": "", "chart_count": 3}]

    def app_file_info(self):
        return {"path": "C:/apps/data.qvf", "size_bytes": 100, "modified": "2026-08-03 14:33:31"}

    def reload_data(self):
        self.reloaded = True
        return {"success": self.reload_ok, "errors": [] if self.reload_ok else ["boom"]}

    def get_tables(self):
        return [{"name": "Orders", "rows": 100, "fields": []}]

    def get_fields(self):
        return []

    def save(self):
        self.saved = True


@pytest.fixture
def engine(monkeypatch):
    fake = FakeEngine()

    def new_session():
        fake.connected = True
        return fake

    monkeypatch.setattr(session, "QlikEngine", new_session)
    session._state.update({
        "engine": fake, "app_name": "data", "model": "test-model",
        "client": None, "messages": [],
    })
    yield fake
    session._state.update({
        "engine": None, "app_name": None, "client": None, "messages": [],
    })


@pytest.fixture
def client(engine):
    return TestClient(web_app.app)


class TestState:

    def test_reports_script_tabs_and_connections(self, client):
        state = client.get("/api/state").json()
        assert state["tabs"] == ["Main", "other"]
        assert state["app"] == "data"
        assert state["connections"][0]["name"] == "dataset"
        assert "LOAD *" in state["script"]

    def test_survives_connections_being_unavailable(self, client, engine):
        from qlik_engine import QlikEngineError

        def boom():
            raise QlikEngineError("no connections")

        engine.list_connections = boom
        assert client.get("/api/state").json()["connections"] == []

    def test_a_dropped_connection_is_rebuilt_rather_than_reported(self, client):
        session._state["engine"] = None

        response = client.get("/api/state")

        assert response.status_code == 200
        assert response.json()["app"] == "data"

    def test_a_connection_that_cannot_be_rebuilt_says_so_and_offers_the_button(
            self, client, monkeypatch):
        from qlik_engine import QlikConnectionError

        def unreachable():
            raise QlikConnectionError("Could not connect to the Qlik Engine")

        monkeypatch.setattr(session, "QlikEngine", unreachable)
        session._state["engine"] = None

        response = client.get("/api/state")

        assert response.status_code == 503
        assert response.headers.get("X-Qlik-Reconnect") == "1"
        assert "lost" in response.json()["detail"]


class TestScriptSaving:

    def test_saves_the_buffer(self, client, engine):
        response = client.put("/api/script", json={"content": "///$tab New\r\nTRACE x;\r\n"})
        assert response.status_code == 200
        assert response.json()["tabs"] == ["New"]
        assert "TRACE x;" in engine.script

    def test_a_script_that_does_not_parse_is_rejected(self, client, engine):
        engine.rejected = "BROKEN"
        response = client.put("/api/script", json={"content": "BROKEN((("})

        assert response.status_code == 400
        assert "syntax error" in response.json()["detail"]
        assert engine.script == SCRIPT, "a broken script was left in the app"


class TestReload:

    def test_reports_tables_and_row_counts(self, client, engine):
        result = client.post("/api/reload").json()
        assert engine.reloaded is True
        assert result["success"] is True
        assert result["total_rows"] == 100

    def test_reports_failure_with_errors(self, client, engine):
        engine.reload_ok = False
        result = client.post("/api/reload").json()
        assert result["success"] is False
        assert result["errors"] == ["boom"]


class TestChat:

    def test_detects_that_the_assistant_changed_the_script(self, client, engine, monkeypatch):
        def fake_agent(client_, model, engine_, messages, **kwargs):
            kwargs["on_call"]("write_script", {})
            engine_.set_script("///$tab Loader\r\nLOAD 1;\r\n")
            return "Wrote the loader."

        monkeypatch.setattr(web_app, "run_agent", fake_agent)

        result = client.post("/api/chat", json={"message": "write a loader"}).json()

        assert result["script_changed"] is True
        assert result["tabs"] == ["Loader"]
        assert result["actions"] == ["write_script"]
        assert result["reply"] == "Wrote the loader."

    def test_read_only_turn_reports_no_change(self, client, monkeypatch):
        monkeypatch.setattr(
            web_app, "run_agent",
            lambda *a, **kw: "There is one file.",
        )
        result = client.post("/api/chat", json={"message": "what files?"}).json()
        assert result["script_changed"] is False

    def test_a_failing_turn_returns_an_error_not_a_crash(self, client, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("model died")

        monkeypatch.setattr(web_app, "run_agent", boom)
        response = client.post("/api/chat", json={"message": "hi"})

        assert response.status_code == 500
        assert "model died" in response.json()["detail"]


class TestSaveIsReported:
    def test_save_returns_the_file_stamp(self, client):
        result = client.post("/api/save").json()
        assert result["ok"] is True
        assert result["file"]["modified"] == "2026-08-03 14:33:31"

    def test_chat_reports_when_sheets_appeared(self, client, engine, monkeypatch):
        added = {"done": False}

        def fake_agent(*args, **kwargs):
            added["done"] = True
            return "Built it."

        def list_sheets():
            sheets = [{"qId": "SH_1", "title": "Sales", "chart_count": 3}]
            if added["done"]:
                sheets.append({"qId": "SH_2", "title": "New", "chart_count": 4})
            return sheets

        engine.list_sheets = list_sheets
        monkeypatch.setattr(web_app, "run_agent", fake_agent)

        result = client.post("/api/chat", json={"message": "build one"}).json()

        assert result["sheets_changed"] is True
        assert result["file"]["modified"]

    def test_no_note_when_nothing_was_created(self, client, monkeypatch):
        monkeypatch.setattr(web_app, "run_agent", lambda *a, **kw: "Total is 5.")
        assert client.post("/api/chat", json={"message": "total?"}).json()[
            "sheets_changed"
        ] is False


class TestLoadEditorToolPolicy:
    def test_reload_is_withheld_from_the_assistant(self):
        assert "reload_data" not in LOAD_EDITOR_TOOLS

    def test_script_editing_is_available(self):
        for name in ("read_script", "write_script", "build_load_script", "data_sources"):
            assert name in LOAD_EDITOR_TOOLS

    def test_execute_refuses_a_withheld_tool(self):
        result = execute(FakeEngine(), "reload_data", {}, allowed=LOAD_EDITOR_TOOLS)
        assert "not available" in result["error"]

    def test_execute_refuses_destructive_tools_with_no_confirm(self):
        engine = FakeEngine()
        result = execute(engine, "reload_data", {})
        assert result["cancelled"] is True
        assert engine.reloaded is False

    def test_execute_runs_destructive_tools_when_confirmed(self):
        engine = FakeEngine()
        result = execute(engine, "reload_data", {}, confirm=lambda q, action: True)
        assert result["success"] is True
        assert engine.reloaded is True

    def test_only_the_unrecoverable_actions_are_destructive(self):
        assert DESTRUCTIVE == {"reload_data", "delete_sheet"}

    def test_building_and_reading_are_never_gated(self):
        for name in ("query", "data_model", "list_charts", "build_dashboard",
                     "create_chart", "edit_chart", "analyze_sheet", "save"):
            assert name not in DESTRUCTIVE


class TestWithheldIsNotMissing:
    def test_a_withheld_tool_explains_itself(self):
        from chat_tools import execute

        result = execute(None, "delete_sheet", {"sheet": "S"}, allowed={"query"})
        assert "not available here" in result["error"]
        assert "switched off" in result["note"]
        assert "not a missing capability" in result["note"]

    def test_an_invented_tool_gets_no_note(self):
        from chat_tools import execute

        result = execute(None, "teleport", {}, allowed={"query"})
        assert "not available here" in result["error"]
        assert "note" not in result


class TestDeletingSheetsFromTheBrowser:
    def test_delete_sheet_is_withheld_from_the_browser(self):
        from web_app import ASSISTANT_TOOLS

        assert "delete_sheet" not in ASSISTANT_TOOLS

    def test_the_refusal_says_switched_off_not_missing(self):
        from chat_tools import execute
        from web_app import ASSISTANT_TOOLS

        result = execute(None, "delete_sheet", {"sheet": "S"},
                         allowed=ASSISTANT_TOOLS)
        assert "switched off" in result["note"]
        assert "not a missing capability" in result["note"]

    def test_the_quieter_destructive_actions_stay_gated(self):
        from web_app import _may_run

        _may_run = _may_run(session.system())
        assert _may_run("q", "write_script") is False
        assert _may_run("q", "delete_sheet") is False
