"""The AI Data Load Editor's HTTP layer, against a fake Qlik engine."""

import pytest
from fastapi.testclient import TestClient

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
def engine():
    """Install a fake engine into the shared session everything now uses."""
    fake = FakeEngine()
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

    def test_reports_not_connected(self, client):
        session._state["engine"] = None
        assert client.get("/api/state").status_code == 503


class TestScriptSaving:

    def test_saves_the_buffer(self, client, engine):
        response = client.put("/api/script", json={"content": "///$tab New\r\nTRACE x;\r\n"})
        assert response.status_code == 200
        assert response.json()["tabs"] == ["New"]
        assert "TRACE x;" in engine.script

    def test_a_script_that_does_not_parse_is_rejected(self, client, engine):
        """The engine rolls back; the editor must surface that, not claim success."""
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
    """Qlik Desktop shows a cached copy of an open app, so a created sheet
    looks like it never happened. The file's timestamp proves otherwise."""

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
    """In the editor, loading data is a button the person presses - the same
    arrangement as Qlik's own Data load editor."""

    def test_reload_is_withheld_from_the_assistant(self):
        assert "reload_data" not in LOAD_EDITOR_TOOLS

    def test_script_editing_is_available(self):
        for name in ("read_script", "write_script", "build_load_script", "data_sources"):
            assert name in LOAD_EDITOR_TOOLS

    def test_execute_refuses_a_withheld_tool(self):
        result = execute(FakeEngine(), "reload_data", {}, allowed=LOAD_EDITOR_TOOLS)
        assert "not available" in result["error"]

    def test_execute_refuses_destructive_tools_with_no_confirm(self):
        """No confirmation callback means no way to say yes, so it must not run."""
        engine = FakeEngine()
        result = execute(engine, "reload_data", {})
        assert result["cancelled"] is True
        assert engine.reloaded is False

    def test_execute_runs_destructive_tools_when_confirmed(self):
        engine = FakeEngine()
        result = execute(engine, "reload_data", {}, confirm=lambda q: True)
        assert result["success"] is True
        assert engine.reloaded is True

    def test_reload_is_the_only_destructive_action(self):
        assert DESTRUCTIVE == {"reload_data"}


class FakeOllama:
    CAPS = {
        "gemma4:26b": ["completion", "tools"],
        "qwen2.5-coder:7b": ["completion", "tools"],
        "phi4:14b": ["completion"],
        "kimi:cloud": ["completion", "tools"],
    }
    SIZES = {"gemma4:26b": 18e9, "qwen2.5-coder:7b": 4.7e9, "phi4:14b": 9.1e9, "kimi:cloud": 0}

    def list(self):
        return {"models": [{"model": n, "size": self.SIZES[n]} for n in self.CAPS]}

    def show(self, model):
        return {"capabilities": self.CAPS.get(model, [])}


@pytest.fixture
def with_models(engine):
    session._state["client"] = FakeOllama()
    session._state["model"] = "gemma4:26b"
    return engine


class TestOptions:

    def test_lists_apps_and_models(self, client, with_models, monkeypatch):
        monkeypatch.setattr(
            with_models, "list_apps",
            lambda: [{"name": "data"}, {"name": "data1"}], raising=False,
        )
        options = client.get("/api/options").json()

        assert options["apps"] == ["data", "data1"]
        assert options["model"] == "gemma4:26b"

    def test_marks_models_that_cannot_be_used(self, client, with_models, monkeypatch):
        monkeypatch.setattr(with_models, "list_apps", lambda: [], raising=False)
        models = {m["name"]: m for m in client.get("/api/options").json()["models"]}

        assert models["phi4:14b"]["usable"] is False
        assert "cannot call tools" in models["phi4:14b"]["reason"]
        assert models["qwen2.5-coder:7b"]["usable"] is True

    def test_cloud_models_are_offered_but_flagged(self, client, with_models, monkeypatch):
        """Blocking them outright was wrong - some are free and far faster
        than a big local model. They are the one choice that sends data off
        the machine, so they are labelled and never auto-picked."""
        monkeypatch.setattr(with_models, "list_apps", lambda: [], raising=False)
        models = {m["name"]: m for m in client.get("/api/options").json()["models"]}

        cloud = models["kimi:cloud"]
        assert cloud["usable"] is True
        assert cloud["cloud"] is True
        assert cloud["recommended"] is False
        assert "data leaves this machine" in cloud["reason"]

    def test_smallest_usable_model_is_offered_first(self, client, with_models, monkeypatch):
        """On a machine also running Qlik, fitting in memory beats scoring well."""
        monkeypatch.setattr(with_models, "list_apps", lambda: [], raising=False)
        names = [m["name"] for m in client.get("/api/options").json()["models"]]
        assert names[0] == "qwen2.5-coder:7b"

    def test_survives_apps_being_unlistable(self, client, with_models, monkeypatch):
        from qlik_engine import QlikEngineError

        def boom():
            raise QlikEngineError("nope")

        monkeypatch.setattr(with_models, "list_apps", boom, raising=False)
        assert client.get("/api/options").json()["apps"] == ["data"]


class TestSelect:

    def test_switches_model(self, client, with_models):
        assert client.post("/api/select", json={"model": "qwen2.5-coder:7b"}).status_code == 200
        assert session.model() == "qwen2.5-coder:7b"

    def test_refuses_a_model_that_cannot_call_tools(self, client, with_models):
        response = client.post("/api/select", json={"model": "phi4:14b"})
        assert response.status_code == 400
        assert "cannot call tools" in response.json()["detail"]
        assert session.model() == "gemma4:26b"

    def test_switches_app(self, client, with_models, monkeypatch):
        opened = []
        monkeypatch.setattr(with_models, "open_app", opened.append, raising=False)

        assert client.post("/api/select", json={"app": "data1"}).status_code == 200
        assert opened == ["data1"]
        assert session.app_name() == "data1"

    def test_switching_app_clears_the_conversation(self, client, with_models, monkeypatch):
        """Otherwise it answers confidently about tables from the old app."""
        monkeypatch.setattr(with_models, "open_app", lambda name: None, raising=False)
        session._state["messages"] = [{"role": "user", "content": "about data"}]

        client.post("/api/select", json={"app": "data1"})

        assert [m["role"] for m in session.messages()] == ["system"]

    def test_a_failed_app_switch_keeps_the_current_app(self, client, with_models, monkeypatch):
        from qlik_engine import QlikEngineError

        def boom(name):
            raise QlikEngineError("App already open")

        monkeypatch.setattr(with_models, "open_app", boom, raising=False)
        response = client.post("/api/select", json={"app": "locked"})

        assert response.status_code == 400
        assert "App already open" in response.json()["detail"]
        assert session.app_name() == "data"

    def test_selecting_the_same_values_is_a_no_op(self, client, with_models, monkeypatch):
        opened = []
        monkeypatch.setattr(with_models, "open_app", opened.append, raising=False)
        client.post("/api/select", json={"app": "data", "model": "gemma4:26b"})
        assert opened == []


class TestPageIsSelfContained:
    """It has to work with no internet - the whole point is staying local."""

    def test_index_exists(self):
        assert web_app.INDEX.is_file()

    @pytest.mark.parametrize("needle", ["http://", "https://", "cdn."])
    def test_no_external_resources(self, needle):
        html = web_app.INDEX.read_text(encoding="utf-8")
        # Ignore the doctype/lang boilerplate and any comment prose.
        for line in html.splitlines():
            if needle in line and "<html" not in line and "//" != line.strip()[:2]:
                assert "placeholder" in line or "e.g." in line, f"external reference: {line.strip()[:90]}"
