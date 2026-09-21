"""A stand-in for the Qlik Sense Engine.

The test server has no Qlik licence, so the real engine cannot be reached.
This speaks enough of the Engine JSON-RPC protocol to drive the whole stack -
and, which is the entire point, it decides what a connection may see from the
``X-Qlik-User`` header that connection was opened with. If the header is
missing, wrong, or shared between two people, these tests notice.

It is installed by patching ``websocket.create_connection``, so everything
above it runs for real: ``QlikEngine.connect()`` builds the URL, the header
and the SSL options exactly as it would against Qlik.
"""

import json

import websocket


def parse_identity(header_lines):
    """'X-Qlik-User: UserDirectory=QS; UserId=bekzat' -> 'QS\\bekzat'.

    Returns None when no usable identity was sent, which is how an
    unauthenticated or desktop-mode connection arrives.
    """
    for line in header_lines or []:
        name, _, value = str(line).partition(":")
        if name.strip().lower() != "x-qlik-user":
            continue

        found = {}
        for part in value.split(";"):
            key, _, item = part.partition("=")
            if item.strip():
                found[key.strip().lower()] = item.strip()

        directory, user_id = found.get("userdirectory"), found.get("userid")
        if directory and user_id:
            return f"{directory}\\{user_id}"
    return None


class World:
    """Who can see which apps, as the QMC's security rules would decide it."""

    def __init__(self, apps_by_identity, service_account=None):
        self.apps_by_identity = {
            self._key(who): list(apps) for who, apps in apps_by_identity.items()
        }
        self.service_account = self._key(service_account) if service_account else None

    @staticmethod
    def _key(who):
        return str(who or "").strip().lower()

    def apps_for(self, identity):
        """Unknown users see nothing - Qlik creates them with no rights."""
        return self.apps_by_identity.get(self._key(identity), [])

    def app_id(self, name):
        return f"app-{name.lower().replace(' ', '-')}"


class Connection:
    """One connection, pinned to the identity its header carried.

    Distinct from ``conftest.FakeEngineSocket``, which is a generic handler map
    bolted straight onto ``engine.ws``. This one is reached through
    ``connect()``, so the header and the URL are real.
    """

    def __init__(self, identity, world, url=""):
        self.identity = identity
        self.world = world
        self.url = url
        self.closed = False
        self.sent = []
        self._replies = []
        self._next_handle = 0

    # -- the websocket surface QlikEngine uses -----------------------------

    def settimeout(self, _seconds):
        pass

    def send(self, raw):
        if self.closed:
            raise websocket.WebSocketConnectionClosedException("socket is closed")
        request = json.loads(raw)
        self.sent.append(request)
        self._replies.append(self._answer(request))

    def recv(self):
        if self.closed:
            raise websocket.WebSocketConnectionClosedException("socket is closed")
        if not self._replies:
            raise websocket.WebSocketTimeoutException("nothing to read")
        return json.dumps(self._replies.pop(0))

    def close(self):
        self.closed = True

    # -- the protocol ------------------------------------------------------

    def _answer(self, request):
        method = request.get("method")
        reply = {"jsonrpc": "2.0", "id": request.get("id")}

        handler = getattr(self, f"_do_{method}", None)
        if handler is None:
            reply["error"] = {"code": -32601, "message": f"{method} is not supported"}
            return reply

        try:
            reply["result"] = handler(request.get("params") or [])
        except EngineRefusal as refused:
            reply["error"] = {"code": refused.code, "message": str(refused)}
        return reply

    def _do_GetAuthenticatedUser(self, _params):
        if not self.identity:
            return {"qReturn": ""}
        directory, _, user_id = self.identity.partition("\\")
        return {"qReturn": f"UserDirectory={directory}; UserId={user_id}"}

    def _do_GetDocList(self, _params):
        return {
            "qDocList": [
                {
                    "qDocId": self.world.app_id(name),
                    "qTitle": name,
                    "qDocName": f"{name}.qvf",
                }
                for name in self.world.apps_for(self.identity)
            ]
        }

    def _do_OpenDoc(self, params):
        wanted = params[0] if params else ""
        allowed = {
            self.world.app_id(name): name
            for name in self.world.apps_for(self.identity)
        }
        if wanted not in allowed and wanted not in allowed.values():
            raise EngineRefusal(
                f"App not found, or {self.identity or '(nobody)'} may not open it",
                code=1003,
            )

        self._next_handle += 1
        return {
            "qReturn": {
                "qType": "Doc",
                "qHandle": self._next_handle,
                "qGenericId": wanted,
            }
        }


class EngineRefusal(Exception):
    def __init__(self, message, code=1003):
        super().__init__(message)
        self.code = code


class Switchboard:
    """Every connection the code under test opened, for inspection afterwards.

    ``header_ignored`` reproduces the failure this whole exercise exists to
    catch: a server that accepts the connection but resolves every one of them
    to the same user, whatever ``X-Qlik-User`` asked for.
    """

    def __init__(self, world, header_ignored=None):
        self.world = world
        self.header_ignored = header_ignored
        self.sockets = []

    def connect(self, url, timeout=None, header=None, sslopt=None, **_kwargs):
        asked_for = parse_identity(header)
        resolved = self.header_ignored or asked_for

        socket = Connection(resolved, self.world, url=url)
        socket.asked_for = asked_for
        socket.opened_with = {"url": url, "header": list(header or []),
                              "sslopt": sslopt, "timeout": timeout}
        self.sockets.append(socket)
        return socket

    # -- what the tests ask afterwards -------------------------------------

    @property
    def identities(self):
        return [s.identity for s in self.sockets]

    def sockets_for(self, identity):
        key = World._key(identity)
        return [s for s in self.sockets if World._key(s.identity) == key]


def install(monkeypatch, world, certificates_at=None, header_ignored=None):
    """Point QlikEngine at the fake engine and hand back the switchboard.

    ``certificates_at`` writes the three files enterprise mode insists on, so
    ``_enterprise_sslopt()`` runs for real rather than being skipped.
    """
    import qlik_engine

    if certificates_at is not None:
        certificates_at.mkdir(parents=True, exist_ok=True)
        for name in (qlik_engine.CLIENT_CERT, qlik_engine.CLIENT_KEY,
                     qlik_engine.ROOT_CERT):
            (certificates_at / name).write_text("not a real certificate\n")

    switchboard = Switchboard(world, header_ignored=header_ignored)
    monkeypatch.setattr(qlik_engine.websocket, "create_connection",
                        switchboard.connect)
    return switchboard


def enterprise_engines(monkeypatch, certificates_at, host="qlik.test"):
    """Make ``session`` build enterprise engines.

    ``QlikEngine``'s defaults are bound when the module is imported, so
    monkeypatching config afterwards does not reach them. Wrapping the class
    where ``session`` looks it up does.
    """
    import qlik_engine
    import session

    real = qlik_engine.QlikEngine

    def build(**kwargs):
        kwargs.setdefault("mode", qlik_engine.ENTERPRISE)
        kwargs.setdefault("cert_dir", str(certificates_at))
        kwargs.setdefault("host", host)
        kwargs.setdefault("port", 4747)
        kwargs.setdefault("ssl_verify", False)
        return real(**kwargs)

    monkeypatch.setattr(session, "QlikEngine", build)
    monkeypatch.setattr(session, "QLIK_MODE", qlik_engine.ENTERPRISE)
    monkeypatch.setattr(session, "AUTH_ENABLED", True)
    monkeypatch.setattr(session, "ALLOW_SHARED_QLIK_IDENTITY", False)
    monkeypatch.setattr(session, "QLIK_USER_DIRECTORY", "QS")
    monkeypatch.setattr(session, "QLIK_USER_ID", "svc")
    return build
