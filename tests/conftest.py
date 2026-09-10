import json
import os
import sys
import tempfile
import types
from collections import deque
from pathlib import Path

import pytest
import websocket

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_SANDBOX = Path(tempfile.mkdtemp(prefix="qlikai-tests-"))

BASELINE = {
    "QLIK_MODE": "desktop",
    "QLIK_HOST": "localhost",
    "QLIK_PORT": "4848",
    "QLIK_CERT_DIR": "",
    "QLIK_USER_DIRECTORY": "",
    "QLIK_USER_ID": "",
    "APP_NAME": "data",
    "AUTH_ENABLED": "true",
    "ALLOW_SHARED_QLIK_IDENTITY": "false",
    "PASSWORD_MIN": "8",
    "ADMIN_USERNAME": "admin",
    "ADMIN_PASSWORD": "",
    "LDAP_ENABLED": "false",
    "LDAP_SERVER": "",
    "LDAP_USE_SSL": "true",
    "LDAP_START_TLS": "false",
    "LDAP_PORT": "636",
    "LDAP_UPN_SUFFIX": "",
    "LDAP_WINDOWS_DOMAIN": "",
    "LDAP_BASE_DN": "",
    "LDAP_USER_ATTRIBUTE": "",
    "LDAP_BIND_USER": "",
    "LDAP_BIND_PASSWORD": "",
    "LDAP_ADMIN_GROUP": "",
    "LDAP_QLIK_DIRECTORY": "",
    "QRS_ENABLED": "false",
    "OPENAI_BASE_URL": "http://127.0.0.1:9/v1",
    "OPENAI_API_KEY": "",
    "OPENAI_MODEL": "test-model",
    "CHAT_MODEL": "",
    "USERS_DB": str(_SANDBOX / "users.db"),
    "HISTORY_DIR": str(_SANDBOX / "history"),
    "GLOSSARY_FILE": str(_SANDBOX / "glossary.md"),
    "LOG_FILE": "",
    "AUDIT_RETENTION_DAYS": "0",
    "HISTORY_RETENTION_DAYS": "0",
}

for _name, _value in BASELINE.items():
    os.environ.setdefault(_name, _value)


def _neutral_overrides():
    stub = types.ModuleType("chart_overrides")
    stub.CHART_OVERRIDES = {}
    stub.AVAILABLE_TYPES = ()
    stub.__test_stub__ = True
    return stub


NEUTRAL_OVERRIDES = _neutral_overrides()

sys.modules.setdefault("chart_overrides", NEUTRAL_OVERRIDES)
sys.modules.pop("chart_specs", None)

import auth
import glossary
import history
import session
import users
from qlik_engine import QlikEngine

REAL_IDENTIFY = auth.identify


@pytest.fixture(autouse=True)
def isolated_history(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "HISTORY_DIR", str(tmp_path / "history"))


@pytest.fixture(autouse=True)
def isolated_accounts(tmp_path, monkeypatch):
    monkeypatch.setattr(users, "USERS_DB", str(tmp_path / "users.db"))
    monkeypatch.setattr(users, "PBKDF2_ROUNDS", 1_000)
    users.reset_for_tests()
    yield
    users.reset_for_tests()


@pytest.fixture(scope="session")
def live_app():
    from fastapi.testclient import TestClient

    import web_app

    with TestClient(web_app.app, base_url="http://127.0.0.1:8000") as client:
        yield client


@pytest.fixture(autouse=True)
def one_registry():
    session.reset_for_tests()
    yield
    session.reset_for_tests()


@pytest.fixture(autouse=True)
def signed_in(request, isolated_accounts, monkeypatch):
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
    monkeypatch.setattr(glossary, "GLOSSARY_FILE", str(tmp_path / "absent.md"))


class FakeEngineSocket:
    def __init__(self, handlers=None, noisy=False):
        self.handlers = handlers or {}
        self.noisy = noisy
        self.sent = []
        self._outbox = deque()

    def settimeout(self, timeout):
        pass

    def send(self, payload):
        request = json.loads(payload)
        self.sent.append(request)

        if self.noisy:
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

    def requests_for(self, method):
        return [r for r in self.sent if r["method"] == method]

    def push_unsolicited(self, message):
        self._outbox.append(json.dumps(message))


@pytest.fixture
def engine():
    engine = QlikEngine(autoconnect=False)
    engine.ws = FakeEngineSocket()
    engine.app_handle = 1
    engine.app_name = "test-app"
    return engine


@pytest.fixture
def offline_engine():
    return QlikEngine(autoconnect=False)
