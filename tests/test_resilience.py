import sqlite3
import threading
import time

import pytest
import websocket
from fastapi.testclient import TestClient

import session
import users
import web_app
from qlik_engine import (
    QlikConnectionError,
    QlikEngine,
    QlikEngineError,
    QlikNotConnectedError,
)


class DeadSocket:
    def __init__(self, how):
        self.how = how

    def settimeout(self, _):
        pass

    def send(self, _):
        if self.how == "reset-on-send":
            raise ConnectionResetError(10054, "forcibly closed by the remote host")

    def recv(self):
        if self.how == "closed-on-read":
            raise websocket.WebSocketConnectionClosedException("socket is already closed")
        raise websocket.WebSocketException("Connection is already closed.")

    def close(self):
        pass


def engine_on(dead_socket):
    engine = QlikEngine(autoconnect=False)
    engine.ws = dead_socket
    engine.app_handle = 1
    engine.app_name = "Deposits"
    return engine


class TestADeadSocketIsRecognisedAsDead:
    @pytest.mark.parametrize("how", ["reset-on-send", "closed-on-read", "read-failed"])
    def test_every_way_of_dying_ends_disconnected(self, how):
        engine = engine_on(DeadSocket(how))

        with pytest.raises(QlikConnectionError):
            engine.send("GetScript", handle=1)

        assert engine.connected is False

    def test_the_app_it_was_on_is_remembered(self):
        engine = engine_on(DeadSocket("reset-on-send"))

        with pytest.raises(QlikConnectionError):
            engine.send("GetScript", handle=1)

        assert engine.app_name == "Deposits"

    def test_handles_from_the_old_session_are_not_kept(self):
        engine = engine_on(DeadSocket("reset-on-send"))

        with pytest.raises(QlikConnectionError):
            engine.send("GetScript", handle=1)

        assert engine.app_handle is None


class FakeEngine:
    mode = "desktop"

    def __init__(self):
        self.connected = True
        self.app_name = None
        self.opened = []

    def open_app(self, name):
        self.app_name = name
        self.opened.append(name)
        return 1

    def close(self):
        self.connected = False

    def die(self):
        self.connected = False


@pytest.fixture
def engines(monkeypatch):
    made = []

    def build(*args, **kwargs):
        engine = FakeEngine()
        made.append(engine)
        return engine

    monkeypatch.setattr(session, "QlikEngine", build)
    return made


@pytest.fixture
def person(engines):
    sess = session.for_user({"id": 4242, "username": "jsmith",
                             "qlik_directory": "", "qlik_user_id": ""})
    sess.open_app("Deposits")
    return sess


class TestASessionGetsItsConnectionBack:

    def test_a_dropped_connection_is_rebuilt_on_the_next_request(self, person, engines):
        engines[-1].die()
        session._shared["engine"] = None

        engine = person.engine()

        assert engine.connected is True
        assert len(engines) == 2, "it never made a second connection"

    def test_it_comes_back_on_the_same_app(self, person, engines):
        person.open_app("Risk")
        engines[-1].die()
        session._shared["engine"] = None

        person.engine()

        assert person.app_name() == "Risk"
        assert engines[-1].app_name == "Risk"

    def test_a_session_that_opened_nothing_is_still_told_so(self, engines):
        sess = session.for_user({"id": 77, "username": "new",
                                 "qlik_directory": "", "qlik_user_id": ""})

        with pytest.raises(QlikNotConnectedError):
            sess.engine()

    def test_the_failure_is_reported_when_qlik_is_genuinely_down(
            self, person, monkeypatch):
        def unreachable(*args, **kwargs):
            raise QlikConnectionError("Could not connect to the Qlik Engine")

        person.state["engine"] = None
        session._shared["engine"] = None
        monkeypatch.setattr(session, "QlikEngine", unreachable)

        with pytest.raises(session.QlikConnectionLost, match="lost"):
            person.engine()


class TestReconnectingIsRateLimited:
    @pytest.fixture
    def refusing(self, person, monkeypatch):
        attempts = []

        def unreachable(*args, **kwargs):
            attempts.append(time.monotonic())
            raise QlikConnectionError("Could not connect to the Qlik Engine")

        person.state["engine"] = None
        session._shared["engine"] = None
        monkeypatch.setattr(session, "QlikEngine", unreachable)
        return person, attempts

    def test_a_hundred_requests_do_not_make_a_hundred_attempts(self, refusing):
        sess, attempts = refusing

        for _ in range(100):
            with pytest.raises(session.QlikConnectionLost):
                sess.engine()

        assert len(attempts) == 1, f"{len(attempts)} connection attempts for 100 requests"

    def test_the_requests_inside_the_window_still_say_what_is_wrong(self, refusing):
        sess, _ = refusing

        with pytest.raises(session.QlikConnectionLost):
            sess.engine()
        with pytest.raises(session.QlikConnectionLost, match="Could not connect"):
            sess.engine()

    def test_the_cooldown_expires(self, refusing, monkeypatch):
        sess, attempts = refusing
        monkeypatch.setattr(session, "RECONNECT_COOLDOWN_SECONDS", 0.0)

        for _ in range(3):
            with pytest.raises(session.QlikConnectionLost):
                sess.engine()

        assert len(attempts) == 3

    def test_a_person_pressing_the_button_is_not_rate_limited(self, refusing):
        sess, attempts = refusing

        for _ in range(3):
            with pytest.raises(QlikEngineError):
                sess.reconnect()

        assert len(attempts) == 3

    def test_reconnect_succeeds_once_qlik_comes_back(self, refusing, monkeypatch):
        sess, _ = refusing
        with pytest.raises(QlikEngineError):
            sess.engine()

        monkeypatch.setattr(session, "QlikEngine", lambda *a, **k: FakeEngine())

        assert sess.reconnect().connected is True
        assert sess.engine().connected is True


class TestRecoveryUnderLoad:

    def test_concurrent_requests_make_one_connection_between_them(
            self, person, engines):
        engines[-1].die()
        session._shared["engine"] = None
        before = len(engines)
        errors = []
        start = threading.Barrier(20)

        def request():
            start.wait()
            try:
                person.engine()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=request) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert errors == []
        assert len(engines) - before == 1, "one reconnect for twenty requests"


class TestClosingIdleSessionsDoesNotStallEverybody:
    @pytest.fixture
    def slow_to_close(self, monkeypatch):
        class SlowToClose:
            mode = "desktop"

            def __init__(self, *args, **kwargs):
                self.connected = True
                self.app_name = None

            def open_app(self, name):
                self.app_name = name
                return 1

            def close(self):
                time.sleep(0.3)
                self.connected = False

        monkeypatch.setattr(session, "QlikEngine", SlowToClose)
        monkeypatch.setattr(session, "MAX_USER_SESSIONS", 3)
        monkeypatch.setattr(session, "USER_SESSION_IDLE_MINUTES", 0)
        for number in range(1, 7):
            sess = session.for_user({"id": number, "username": f"u{number}",
                                     "qlik_directory": "", "qlik_user_id": ""})
            sess.open_app("Deposits")
            sess.touched = time.monotonic() - 3600

    def test_an_unrelated_request_does_not_wait_for_the_closing(self, slow_to_close):
        waits = []

        def someone_elses_request():
            start = time.perf_counter()
            session.for_user({"id": 2, "username": "u2",
                              "qlik_directory": "", "qlik_user_id": ""})
            waits.append(time.perf_counter() - start)

        reaping = threading.Thread(
            target=lambda: session.for_user({"id": 99, "username": "newcomer",
                                             "qlik_directory": "",
                                             "qlik_user_id": ""}))
        reaping.start()
        time.sleep(0.02)

        threads = [threading.Thread(target=someone_elses_request) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        reaping.join(timeout=30)

        settled = sorted(waits)[len(waits) // 2]
        assert settled < 0.1, f"the typical request waited {settled:.2f}s on a reap"


class TestQlikBeingDownDoesNotStopTheServer:
    @pytest.fixture
    def qlik_is_down(self, monkeypatch):
        def refuse(name=None):
            raise QlikConnectionError("Could not connect to the Qlik Engine")

        monkeypatch.setattr(session, "open_app", refuse)
        started = {}
        monkeypatch.setattr(web_app.uvicorn, "run",
                            lambda app, **kw: started.setdefault("served", True))
        return started

    def test_it_starts_anyway(self, qlik_is_down):
        assert web_app.main(["--host", "127.0.0.1"]) == 0
        assert qlik_is_down["served"] is True

    def test_failing_fast_is_still_available(self, qlik_is_down, monkeypatch):
        monkeypatch.setattr(web_app, "REQUIRE_QLIK_AT_STARTUP", True)

        assert web_app.main(["--host", "127.0.0.1"]) == 1
        assert "served" not in qlik_is_down


class TestADeadDatabaseHandleHeals:
    def test_the_next_call_opens_a_new_one(self):
        users.create("alice", "alice-password-1")
        users.connect().close()

        assert users.by_username("alice") is not None

    def test_a_live_handle_is_not_thrown_away(self):
        first = users.connect()
        assert users.connect() is first

    def test_the_check_costs_almost_nothing(self):
        db = users.connect()
        runs = 20_000

        start = time.perf_counter()
        for _ in range(runs):
            users._usable(db)
        each = (time.perf_counter() - start) / runs

        assert each < 5e-6, f"{each * 1e6:.2f} microseconds per database call"

    def test_a_database_that_is_present_but_unhappy_is_not_mistaken_for_a_dead_handle(self):
        db = users.connect()
        with pytest.raises(sqlite3.Error):
            db.execute("SELECT * FROM there_is_no_such_table")

        assert users._usable(db) is True


@pytest.fixture
def client(engines):
    session.system().state.update({"engine": None, "app_name": None})
    return TestClient(web_app.app)


class TestTheReconnectButton:

    def test_it_rebuilds_the_connection(self, client, monkeypatch):
        sess = session.system()
        sess.open_app("Deposits")
        sess.state["engine"].die()
        session._shared["engine"] = None

        response = client.post("/api/reconnect")

        assert response.status_code == 200
        assert response.json()["qlik_connected"] is True

    def test_it_says_so_when_qlik_is_still_down(self, client, monkeypatch):
        sess = session.system()
        sess.open_app("Deposits")
        sess.state["engine"] = None
        session._shared["engine"] = None

        def unreachable(*args, **kwargs):
            raise QlikConnectionError("Could not connect to the Qlik Engine")

        monkeypatch.setattr(session, "QlikEngine", unreachable)
        response = client.post("/api/reconnect")

        assert response.status_code == 503
        assert response.headers.get("X-Qlik-Reconnect") == "1"

    def test_it_is_written_down(self, client, signed_in):
        session.system().open_app("Deposits")
        client.post("/api/reconnect")

        actions = [row["action"] for row in users.audit_trail()]
        assert "qlik.reconnected" in actions


class TestTheHealthView:

    def test_it_reports_what_is_reachable(self, client):
        session.system().open_app("Deposits")

        body = client.get("/api/health").json()

        assert body["qlik_connected"] is True
        assert body["app"] == "Deposits"

    def test_it_does_not_raise_when_things_are_broken(self, client):
        sess = session.system()
        sess.open_app("Deposits")
        sess.state["engine"].die()

        body = client.get("/api/health").json()

        assert body["qlik_connected"] is False

    def test_the_service_probe_stays_flat(self, client):
        session.system().state["engine"] = None

        assert client.get("/healthz").status_code == 200


class TestTheAdministratorsButton:
    def test_it_reconnects_every_session(self, client, engines):
        people = []
        for number in (11, 12, 13):
            sess = session.for_user({"id": number, "username": f"u{number}",
                                     "qlik_directory": "", "qlik_user_id": ""})
            sess.open_app("Deposits")
            people.append(sess)
        for sess in people:
            sess.state["engine"].die()
        session._shared["engine"] = None

        body = client.post("/api/admin/reconnect-all").json()

        assert len(body["reconnected"]) >= 3
        assert body["failed"] == []
        assert all(sess.connected() for sess in people)

    def test_one_broken_session_does_not_stop_the_others(self, client, engines,
                                                         monkeypatch):
        good = session.for_user({"id": 21, "username": "good",
                                 "qlik_directory": "", "qlik_user_id": ""})
        good.open_app("Deposits")
        bad = session.for_user({"id": 22, "username": "bad",
                                "qlik_directory": "", "qlik_user_id": ""})
        bad.open_app("Deposits")
        bad.state["engine"].die()

        def refuse():
            raise QlikConnectionError("no")

        monkeypatch.setattr(bad, "reconnect", refuse)
        body = client.post("/api/admin/reconnect-all").json()

        assert [f["session"] for f in body["failed"]] == [bad.key]
        assert good.key in body["reconnected"]

    def test_it_shows_every_session_s_connection(self, client, engines):
        sess = session.for_user({"id": 31, "username": "kate",
                                 "qlik_directory": "", "qlik_user_id": ""})
        sess.open_app("Deposits")

        body = client.get("/api/admin/health").json()

        assert any(row["user"] == "kate" and row["qlik_connected"]
                   for row in body["sessions"])
