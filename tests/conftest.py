"""Shared fixtures. Nothing here touches a real Qlik or Ollama instance."""

import json
import sys
from collections import deque
from pathlib import Path

import pytest
import websocket

# The project is a flat set of modules rather than an installed package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qlik_engine import QlikEngine  # noqa: E402


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
