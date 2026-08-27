"""Shared fixtures. Nothing here touches a real Qlik or Ollama instance."""

import json
import sys
from collections import deque
from pathlib import Path

import pytest
import websocket

# The project is a flat set of modules rather than an installed package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import auth  # noqa: E402
import glossary  # noqa: E402
import history  # noqa: E402
import session  # noqa: E402
import users  # noqa: E402
from qlik_engine import QlikEngine  # noqa: E402

# Captured before anything patches it, so the tests that exercise the login
# itself can put the real one back.
REAL_IDENTIFY = auth.identify


@pytest.fixture(autouse=True)
def isolated_history(tmp_path, monkeypatch):
    """Keep every test's chat history out of the developer's real home.

    session.persist() runs on any turn the web tests drive, so without this
    a test run quietly files itself into ~/.qlik-ai/history.
    """
    monkeypatch.setattr(history, "HISTORY_DIR", str(tmp_path / "history"))


@pytest.fixture(autouse=True)
def isolated_accounts(tmp_path, monkeypatch):
    """A fresh account database per test, never the developer's real one.

    The work factor is turned right down for the suite. It is deliberately
    expensive in production - that is the entire point of PBKDF2 - and a
    fixture that creates a user for every test would otherwise spend minutes
    of every run proving that a slow hash is slow.
    """
    monkeypatch.setattr(users, "USERS_DB", str(tmp_path / "users.db"))
    monkeypatch.setattr(users, "PBKDF2_ROUNDS", 1_000)
    users.reset_for_tests()
    yield
    users.reset_for_tests()


@pytest.fixture(scope="session")
def live_app():
    """A client with the app's lifespan actually running.

    Only for the tests that drive `/mcp`, and shared across the whole run
    because it has to be: the MCP session manager refuses to start twice in
    one process, so a second `with TestClient(app)` anywhere raises. Every
    other test uses a plain TestClient and never enters the lifespan.
    """
    from fastapi.testclient import TestClient

    import web_app

    # A real host, because MCP's transport security checks the Host header
    # against the address it is serving (DNS-rebinding protection) and
    # TestClient's default "testserver" is rejected with a 421.
    with TestClient(web_app.app, base_url="http://127.0.0.1:8000") as client:
        yield client


@pytest.fixture(autouse=True)
def one_registry():
    """Don't let one test's sessions or shared connection reach the next.

    The shared engine especially: it is one slot for the whole process, so a
    fake left in it by an earlier test is picked up by a later one as a live
    connection - which showed up as a test that passed alone and failed in
    the suite.
    """
    session.reset_for_tests()
    yield
    session.reset_for_tests()


@pytest.fixture(autouse=True)
def signed_in(request, isolated_accounts, monkeypatch):
    """Run every web test as a signed-in administrator.

    Only the credential *check* is stood in for - the gate, the dependencies,
    the per-user session lookup and the audit trail are all the real ones. So
    a test that drives an endpoint is testing that endpoint rather than the
    login form, and the login form has tests of its own.

    Those tests mark themselves `live_auth` to get the real identify() back
    and an empty account database to work in.

    The administrator's Session *is* the system session, which is what lets
    the older fixtures go on setting up `session._state` and have the request
    see what they set.
    """
    if request.node.get_closest_marker("live_auth"):
        monkeypatch.setattr(auth, "identify", REAL_IDENTIFY)
        yield None
        return

    admin = users.create("tester", "test-password-1", role=users.ADMIN,
                         display_name="Tester")
    session._sessions[session.key_for(admin)] = session.system()
    monkeypatch.setattr(auth, "identify",
                        lambda request_: auth.Identity(admin, ip="testclient"))
    yield admin


@pytest.fixture(autouse=True)
def no_local_glossary(tmp_path, monkeypatch):
    """Keep a developer's own glossary.md out of the system prompt.

    system_prompt() reads it from disk, so without this the suite would pass
    or fail depending on whether the machine running it happens to have one.
    """
    monkeypatch.setattr(glossary, "GLOSSARY_FILE", str(tmp_path / "absent.md"))


class FakeEngineSocket:
    """Stand-in for a websocket to the Qlik Engine.

    Answers each request from a handler map, echoing the request's id the way
    the real engine does, and can interleave unsolicited notifications so the
    id-matching logic in QlikEngine._await_response is actually exercised.
    """

    def __init__(self, handlers=None, noisy=False):
        self.handlers = handlers or {}
        self.noisy = noisy
        self.sent = []
        self._outbox = deque()

    # -- websocket API surface QlikEngine uses -------------------------

    def settimeout(self, timeout):
        pass

    def send(self, payload):
        request = json.loads(payload)
        self.sent.append(request)

        if self.noisy:
            # No "id" - the real engine emits these between responses.
            self._outbox.append(json.dumps(
                {"jsonrpc": "2.0", "method": "OnConnected", "params": {}}
            ))

        handler = self.handlers.get(request["method"])
        result = handler(request) if callable(handler) else handler
        if result is None:
            result = {}

        if "error" in result:
            message = {"jsonrpc": "2.0", "id": request["id"], "error": result["error"]}
        else:
            message = {"jsonrpc": "2.0", "id": request["id"], "result": result}

        self._outbox.append(json.dumps(message))

    def recv(self):
        if not self._outbox:
            raise websocket.WebSocketTimeoutException("nothing queued")
        return self._outbox.popleft()

    def close(self):
        pass

    # -- test helpers --------------------------------------------------

    def requests_for(self, method):
        return [r for r in self.sent if r["method"] == method]

    def push_unsolicited(self, message):
        self._outbox.append(json.dumps(message))


@pytest.fixture
def engine():
    """A QlikEngine wired to a fake socket, with an app already 'open'."""
    engine = QlikEngine(autoconnect=False)
    engine.ws = FakeEngineSocket()
    engine.app_handle = 1
    engine.app_name = "test-app"
    return engine


@pytest.fixture
def offline_engine():
    """A QlikEngine that never connects - for pure layout/helper logic."""
    return QlikEngine(autoconnect=False)
