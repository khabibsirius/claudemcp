import pytest
from fastapi.testclient import TestClient

import history
import session
import users
import web_app
from qlik_engine import QlikNotConnectedError

pytestmark = pytest.mark.live_auth


class FakeEngine:
    mode = "desktop"

    def __init__(self, identity=None):
        self.connected = True
        self.app_name = None
        self.identity = identity
        self.opened = []
        self.closed = False

    def open_app(self, name):
        self.app_name = name
        self.opened.append(name)
        return 1

    def close(self):
        self.connected = False
        self.closed = True

    def list_apps(self):
        return [{"name": "sales"}, {"name": "risk"}]

    def get_script(self):
        return "///$tab Main\r\nTRACE hi;\r\n"

    def set_script(self, script, validate=True):
        return script

    def list_connections(self):
        return []

    def list_sheets(self):
        return []

    def get_fields(self):
        return []

    def get_tables(self):
        return []

    def app_file_info(self):
        return {"path": "x.qvf", "size_bytes": 1, "modified": "now"}

    def reload_data(self):
        return {"success": True, "errors": []}

    def save(self):
        return True


@pytest.fixture
def two_people():
    return (
        users.create("alice", "alice-password-1", display_name="Alice",
                     qlik_directory="BANK", qlik_user_id="alice"),
        users.create("bob", "bob-password-1", display_name="Bob",
                     qlik_directory="BANK", qlik_user_id="bob"),
    )


@pytest.fixture
def engines(monkeypatch):
    made = []

    def build(*args, **kwargs):
        engine = FakeEngine(identity=(kwargs.get("user_directory"),
                                      kwargs.get("user_id")))
        made.append(engine)
        return engine

    monkeypatch.setattr(session, "QlikEngine", build)
    return made


class TestConversationsAreSeparate:

    def test_two_people_are_not_in_the_same_conversation(self, two_people, engines):
        alice, bob = two_people
        one = session.for_user(alice)
        other = session.for_user(bob)

        one.messages().append({"role": "user", "content": "what is our exposure?"})

        assert [m["content"] for m in other.messages()
                if m["role"] == "user"] == []
        assert one.messages() is not other.messages()

    def test_they_have_their_own_chat_ids(self, two_people, engines):
        alice, bob = two_people
        one, other = session.for_user(alice), session.for_user(bob)
        one.reset_chat()
        other.reset_chat()
        assert one.chat_id() != other.chat_id()

    def test_a_conversation_is_filed_under_its_owner(self, two_people, engines):
        alice, bob = two_people
        one = session.for_user(alice)
        one.reset_chat()
        one.messages().append({"role": "user", "content": "hello"})
        one.persist()

        assert history.listing(history.owner_key(alice["id"]))
        assert history.listing(history.owner_key(bob["id"])) == []

    def test_the_sidebar_shows_only_your_own(self, two_people, engines):
        alice, bob = two_people
        for person, question in ((alice, "alice asked this"), (bob, "bob asked that")):
            sess = session.for_user(person)
            sess.reset_chat()
            sess.messages().append({"role": "user", "content": question})
            sess.persist()

        titles = [c["title"] for c in session.for_user(alice).listing()]
        assert titles == ["alice asked this"]

    def test_an_administrator_sees_across_everybody(self, two_people, engines):
        alice, bob = two_people
        for person, question in ((alice, "alice asked this"), (bob, "bob asked that")):
            sess = session.for_user(person)
            sess.reset_chat()
            sess.messages().append({"role": "user", "content": question})
            sess.persist()

        titles = sorted(c["title"] for c in history.all_listing())
        assert titles == ["alice asked this", "bob asked that"]


class TestSettingsAreSeparate:

    def test_the_reload_permission_is_per_person(self, two_people, engines):
        alice, bob = two_people
        session.for_user(alice).set_allow_reload(False)
        assert session.for_user(bob).allow_reload() is True

    def test_the_language_is_per_person(self, two_people, engines):
        alice, bob = two_people
        session.for_user(alice).set_language("ru")
        assert session.for_user(alice).language() == "ru"
        assert session.for_user(bob).language() == ""

    def test_a_new_session_starts_from_the_server_s_model(self, two_people,
                                                          engines):
        alice, _ = two_people
        session.system().state["model"] = "qwen3.6:35b"

        sess = session.for_user(alice)
        assert sess.model() == "qwen3.6:35b"
        assert sess.assistant_ready() is True

    def test_choosing_a_model_does_not_change_anybody_else_s(self, two_people,
                                                             engines,
                                                             monkeypatch):
        import llm as ollama_client

        monkeypatch.setattr(ollama_client, "supports_tools", lambda c, n: True)
        alice, bob = two_people
        session.system().state["model"] = "shared-default"

        session.for_user(alice).set_model("something-else")
        assert session.for_user(bob).model() == "shared-default"

    def test_the_open_app_is_per_person(self, two_people, engines):
        alice, bob = two_people
        session.for_user(alice).open_app("sales")
        session.for_user(bob).open_app("risk")
        assert session.for_user(alice).app_name() == "sales"
        assert session.for_user(bob).app_name() == "risk"


class TestEnterpriseGivesEveryoneTheirOwnConnection:

    @pytest.fixture(autouse=True)
    def enterprise(self, monkeypatch):
        monkeypatch.setattr(session, "per_user_engines", lambda: True)

    def test_each_session_connects_as_its_own_user(self, two_people, engines):
        alice, bob = two_people
        session.for_user(alice).open_app("sales")
        session.for_user(bob).open_app("sales")

        assert [e.identity for e in engines] == [("BANK", "alice"), ("BANK", "bob")]

    def test_they_are_different_sockets(self, two_people, engines):
        alice, bob = two_people
        assert (session.for_user(alice).open_app("sales")
                is not session.for_user(bob).open_app("sales"))

    def test_they_do_not_wait_on_each_other(self, two_people, engines):
        alice, bob = two_people
        assert session.for_user(alice).lock is not session.for_user(bob).lock

    def test_a_user_with_no_qlik_identity_is_refused(self, engines,
                                                     monkeypatch):
        monkeypatch.setattr(session, "AUTH_ENABLED", True)
        monkeypatch.setattr(session, "ALLOW_SHARED_QLIK_IDENTITY", False)

        nobody = users.create("carol", "carol-password-1")
        sess = session.for_user(nobody)

        assert sess.impersonates is False
        with pytest.raises(session.QlikIdentityMissing):
            sess.open_app("sales")

        assert engines == [], (
            "borrowing the service account is how a plain user came to see "
            "every app the service account can see"
        )

    def test_the_old_fallback_can_be_restored_deliberately(self, engines,
                                                           monkeypatch):
        monkeypatch.setattr(session, "AUTH_ENABLED", True)
        monkeypatch.setattr(session, "ALLOW_SHARED_QLIK_IDENTITY", True)

        nobody = users.create("dmitri", "dmitri-password-1")
        sess = session.for_user(nobody)

        sess.open_app("sales")
        assert engines[0].identity == (None, None)

    def test_changing_someone_s_qlik_identity_drops_their_connection(
            self, two_people, engines):
        alice, _ = two_people
        first = session.for_user(alice)
        first.open_app("sales")

        moved = dict(alice, qlik_user_id="alice.smith")
        again = session.for_user(moved)

        assert again is first
        assert first.state["engine"] is None
        assert engines[0].closed is True


class TestDesktopSharesOneConnection:
    def test_everyone_works_through_one_socket(self, two_people, engines):
        alice, bob = two_people
        session.for_user(alice).open_app("sales")
        session.for_user(bob).open_app("sales")
        assert len(engines) == 1

    def test_they_take_turns_on_one_lock(self, two_people, engines):
        alice, bob = two_people
        assert session.for_user(alice).lock is session.for_user(bob).lock

    def test_the_document_is_re_opened_for_whoever_is_asking(self, two_people,
                                                             engines):
        alice, bob = two_people
        one, other = session.for_user(alice), session.for_user(bob)

        one.open_app("sales")
        other.open_app("risk")

        assert one.engine().app_name == "sales"
        assert other.engine().app_name == "risk"

    def test_the_displaced_user_is_not_told_their_app_closed(self, two_people,
                                                             engines):
        alice, bob = two_people
        one, other = session.for_user(alice), session.for_user(bob)

        one.open_app("sales")
        other.open_app("risk")

        assert one.connected() is False
        assert one.engine().app_name == "sales"
        assert one.app_name() == "sales"

    def test_nothing_open_is_still_an_error(self, two_people, engines):
        from qlik_engine import QlikNotConnectedError

        alice, _ = two_people
        with pytest.raises(QlikNotConnectedError, match="No app is open"):
            session.for_user(alice).engine()


class TestOverHttp:

    @pytest.fixture
    def clients(self, two_people, engines):
        made = {}
        for username, password in (("alice", "alice-password-1"),
                                   ("bob", "bob-password-1")):
            client = TestClient(web_app.app)
            assert client.post("/api/login", json={
                "username": username, "password": password}).status_code == 200
            made[username] = client
        return made

    def test_a_first_visit_opens_the_app_rather_than_reporting_none(
            self, clients, engines):
        state = clients["alice"].get("/api/state")
        assert state.status_code == 200
        assert state.json()["app"] == "data"

    def test_a_session_that_lost_its_engine_gets_its_own_app_back(
            self, clients, two_people, engines):
        alice, _ = two_people
        clients["alice"].get("/api/state")
        sess = session.for_user(alice)
        sess.state["app_name"] = "risk"
        sess.state["engine"] = None
        sess._displaced = False

        response = clients["alice"].get("/api/state")

        assert response.status_code == 200
        assert response.json()["app"] == "risk"

    def test_a_session_that_never_opened_anything_is_told_so(
            self, clients, two_people, engines):
        alice, _ = two_people
        sess = session.for_user(alice)
        sess.state["engine"] = None
        sess.state["app_name"] = None

        with pytest.raises(QlikNotConnectedError):
            sess.engine()

    def test_each_browser_is_told_who_it_is(self, clients, engines):
        for username, client in clients.items():
            assert client.get("/api/me").json()["username"] == username

    def test_one_person_s_chat_does_not_appear_in_the_other_s_sidebar(
            self, clients, two_people, engines):
        alice, bob = two_people
        sess = session.for_user(alice)
        sess.reset_chat()
        sess.messages().append({"role": "user", "content": "alice's question"})
        sess.persist()

        assert clients["bob"].get("/api/chats").json()["chats"] == []
        assert len(clients["alice"].get("/api/chats").json()["chats"]) == 1

    def test_a_guessed_chat_id_does_not_reach_another_person_s_conversation(
            self, clients, two_people, engines):
        alice, _ = two_people
        sess = session.for_user(alice)
        sess.reset_chat()
        sess.messages().append({"role": "user", "content": "alice's question"})
        sess.persist()
        chat_id = sess.chat_id()

        assert clients["alice"].get(f"/api/chats/{chat_id}").status_code == 200
        assert clients["bob"].get(f"/api/chats/{chat_id}").status_code == 404

    def test_deleting_is_scoped_to_your_own(self, clients, two_people, engines):
        alice, _ = two_people
        sess = session.for_user(alice)
        sess.reset_chat()
        sess.messages().append({"role": "user", "content": "alice's question"})
        sess.persist()
        chat_id = sess.chat_id()

        clients["bob"].delete(f"/api/chats/{chat_id}")
        assert history.load(chat_id, history.owner_key(alice["id"])) is not None

    def test_the_reload_switch_is_not_shared(self, clients, engines):
        alice = clients["alice"].post("/api/select", json={"allow_reload": False})
        bob = clients["bob"].post("/api/select", json={})

        assert alice.json()["allow_reload"] is False
        assert bob.json()["allow_reload"] is True

    def test_the_language_is_not_shared(self, clients, engines):
        clients["alice"].post("/api/select", json={"language": "ru"})
        assert clients["bob"].post("/api/select", json={}).json()["language"] == ""


class TestTheSystemSession:
    def test_the_module_functions_are_the_system_session(self, engines):
        assert session._state is session.system().state

    def test_it_is_not_any_user_s_session(self, two_people, engines):
        alice, _ = two_people
        assert session.for_user(alice) is not session.system()

    def test_it_files_conversations_where_it_always_did(self, engines):
        assert session.system().owner is None
