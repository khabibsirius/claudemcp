"""The shared session.

The web UI, the assistant and the MCP tools all work on one open app. Qlik
Sense Desktop allows only one session per app, so two connections would lock
each other out - sharing is not an optimisation, it is the only thing that
works.
"""

import pytest

import mcp_server
import session
from ollama_client import OllamaError
from qlik_engine import QlikEngineError, QlikNotConnectedError


class FakeEngine:
    connected = True
    mode = "desktop"

    def __init__(self):
        self.opened = []

    def open_app(self, name):
        self.opened.append(name)

    def list_apps(self):
        return [{"id": "1", "name": "data"}, {"id": "2", "name": "data1"}]

    def get_fields(self):
        return [{"name": "Sales", "tags": [], "tables": [], "cardinality": 5}]

    def list_sheets(self):
        return []

    def close(self):
        self.connected = False


class FakeOllama:
    def __init__(self, caps=None):
        self.caps = caps or {"good:1b": ["tools"], "bad:1b": ["completion"]}

    def list(self):
        return {"models": [{"model": n, "size": 1e9} for n in self.caps]}

    def show(self, model):
        return {"capabilities": self.caps.get(model, [])}


@pytest.fixture
def clean():
    session._state.update({
        "engine": None, "app_name": None, "model": "test",
        "client": None, "messages": [],
    })
    yield
    session._state.update({"engine": None, "app_name": None, "client": None})


@pytest.fixture
def reconnecting(monkeypatch):
    """One fake engine that comes back after a reconnect.

    Changing app drops the session and opens a new one - Qlik Sense Desktop
    keeps a single app open per engine and releases it only when the socket
    holding it closes. Without this the second open would build a real
    QlikEngine and reach for a live Qlik.
    """
    fake = FakeEngine()

    def new_session():
        fake.connected = True
        return fake

    monkeypatch.setattr(session, "QlikEngine", new_session)
    session._state["engine"] = fake
    return fake


class TestConnection:

    def test_engine_errors_clearly_when_nothing_is_open(self, clean):
        with pytest.raises(QlikNotConnectedError, match="No app is open"):
            session.engine()

    def test_opening_records_the_app(self, clean):
        session._state["engine"] = FakeEngine()
        session.open_app("data")
        assert session.app_name() == "data"
        assert session.connected() is True

    def test_reopening_the_same_app_is_a_no_op(self, clean):
        fake = FakeEngine()
        session._state["engine"] = fake
        session.open_app("data")
        session.open_app("data")
        assert fake.opened == ["data"]

    def test_switching_app_starts_a_new_session(self, clean, reconnecting):
        """Keeping the connection is what made the app impossible to change:
        Qlik Sense Desktop keeps one app open per engine and frees it only
        when the socket holding it closes, so the next open was refused with
        "a document is already open"."""
        session.open_app("data")
        dropped = []
        reconnecting.close = lambda: dropped.append(True)

        session.open_app("data1")

        assert dropped, "the session holding the old app was not dropped"
        assert reconnecting.opened == ["data", "data1"]
        assert session.app_name() == "data1"

    def test_switching_app_resets_the_conversation(self, clean, reconnecting):
        """Otherwise it answers confidently about the previous app's fields."""
        session.open_app("data")
        session.messages().append({"role": "user", "content": "about data"})

        session.open_app("data1")

        assert [m["role"] for m in session.messages()] == ["system"]

    def test_a_failed_first_open_leaves_nothing_half_connected(self, clean):
        class Refuses(FakeEngine):
            def open_app(self, name):
                raise QlikNotConnectedError("App already open")

        session._state["engine"] = Refuses()
        with pytest.raises(QlikNotConnectedError):
            session.open_app("locked")

        assert session.connected() is False
        assert session.app_name() is None


    def test_a_dead_connection_on_switch_tears_down(self, clean, monkeypatch):
        """A connection-type failure means the old app is not usable either,
        so the switch tears down to "nothing open". An app-level failure is
        different - engine.open_app changes nothing until OpenDoc succeeds,
        so that keeps the old app (pinned in test_web_app.py)."""
        class RefusesSecond(FakeEngine):
            def open_app(self, name):
                if self.opened:
                    raise QlikNotConnectedError("switch refused")
                super().open_app(name)

        refuser = RefusesSecond()
        monkeypatch.setattr(session, "QlikEngine", lambda: refuser)
        session._state["engine"] = refuser
        session.open_app("data")

        with pytest.raises(QlikNotConnectedError):
            session.open_app("data1")

        assert session.connected() is False
        assert session.app_name() is None

    def test_a_failed_open_on_a_fresh_connection_tears_down(self, clean):
        """The old app_name outlived its connection: after the socket died,
        a failed open on the replacement engine kept claiming that app, and
        reopening it short-circuited as "already open" against a document
        that was never opened - every later call then failed until restart."""
        class RefusesEverything(FakeEngine):
            def open_app(self, name):
                raise QlikEngineError("no such app")

        stale = FakeEngine()
        session._state["engine"] = stale
        session.open_app("data")
        stale.connected = False          # the engine dropped the socket

        with pytest.raises(QlikEngineError):
            session.open_app("typo")

        assert session.app_name() is None
        assert session.connected() is False


class TestChatFiling:

    def test_switching_apps_files_the_old_chat_under_its_own_app(self, clean, tmp_path,
                                                                 monkeypatch, reconnecting):
        """The chat being replaced was asked against the app that was open
        at the time. It used to be filed under the app being switched TO,
        so reopening it restored it over the wrong data model - and the
        mismatch check saw two matching names and said nothing."""
        import history
        monkeypatch.setattr(history, "HISTORY_DIR", str(tmp_path))

        session.open_app("data")
        session.messages().extend([
            {"role": "user", "content": "about data"},
            {"role": "assistant", "content": "answer"},
        ])
        about_data = session.chat_id()
        session.persist()

        session.open_app("data1")

        assert history.load(about_data)["app"] == "data"

    def test_reopening_a_chat_returns_to_the_app_it_was_about(self, clean, tmp_path,
                                                             monkeypatch, reconnecting):
        import history
        monkeypatch.setattr(history, "HISTORY_DIR", str(tmp_path))

        engine = reconnecting
        session.open_app("data")
        session.messages().extend([
            {"role": "user", "content": "about data"},
            {"role": "assistant", "content": "answer"},
        ])
        about_data = session.chat_id()
        session.persist()
        session.open_app("data1")

        session.load_chat(about_data)

        assert engine.opened[-1] == "data"
        assert session.app_name() == "data"


class TestModel:

    def test_rejects_a_model_that_cannot_call_tools(self, clean):
        session._state["client"] = FakeOllama()
        with pytest.raises(OllamaError, match="cannot call tools"):
            session.set_model("bad:1b")
        assert session.model() == "test"

    def test_accepts_a_capable_model(self, clean):
        session._state["client"] = FakeOllama()
        assert session.set_model("good:1b") == "good:1b"
        assert session.model() == "good:1b"

    def test_resolve_falls_back_and_remembers(self, clean):
        session._state["client"] = FakeOllama()
        chosen, note = session.resolve_model("bad:1b")
        assert chosen == "good:1b"
        assert note
        assert session.model() == "good:1b"


class TestEverythingSharesOneSession:
    """The whole point of the refactor: the MCP tools and the web API are
    looking at the same open app, not two competing connections."""

    def test_mcp_tools_use_the_shared_engine(self, clean):
        fake = FakeEngine()
        session._state["engine"] = fake
        session.open_app("data")

        assert mcp_server._require_engine() is fake

    def test_mcp_open_switches_the_shared_app(self, clean, reconnecting):
        fake = reconnecting
        session.open_app("data")

        mcp_server.qlik_open("data1")

        assert session.app_name() == "data1"
        assert fake.opened == ["data", "data1"]

    def test_mcp_reports_not_connected_the_same_way(self, clean):
        with pytest.raises(QlikNotConnectedError):
            mcp_server._require_engine()


class TestWebAppMountsMcp:

    def test_mcp_is_mounted(self):
        import web_app

        mounts = [r.path for r in web_app.app.routes if hasattr(r, "path")]
        assert "/mcp" in mounts

    def test_assistant_has_the_whole_toolset(self):
        """Reloading is gated by a setting, not withheld - see
        test_reload_permission.py for why that distinction matters."""
        import web_app

        for name in ("build_dashboard", "write_script", "reload_data"):
            assert name in web_app.ASSISTANT_TOOLS
