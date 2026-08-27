"""One session per person, rather than one for the whole server.

Blocker B2 in readiness.md: session.py held a single module-level `_state` -
one engine, one message list, one chat id. Two people using the assistant at
once were in the *same* conversation: each saw the other's questions, and the
second person's request landed in the middle of the first person's context.
"""

import pytest
from fastapi.testclient import TestClient

import history
import session
import users
import web_app
from qlik_engine import QlikNotConnectedError

pytestmark = pytest.mark.live_auth


class FakeEngine:
    """A stand-in that remembers which document it has open.

    That is the part that matters here: the desktop connection is shared, so
    "which app is open" is the thing two sessions can disagree about.
    """

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
    """Every connection this test makes, in the order they were made."""
    made = []

    def build(*args, **kwargs):
        engine = FakeEngine(identity=(kwargs.get("user_directory"),
                                      kwargs.get("user_id")))
        made.append(engine)
        return engine

    monkeypatch.setattr(session, "QlikEngine", build)
    return made


# ----------------------------------------------------------------------
# The conversation
# ----------------------------------------------------------------------

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
        """Which model to use is the person's own setting, but a session
        that begins with none reports the assistant as unavailable until
        they have asked it something - so everybody's first look at the page
        said Ollama was down while it was running perfectly well."""
        alice, _ = two_people
        session.system().state["model"] = "qwen3.6:35b"

        sess = session.for_user(alice)
        assert sess.model() == "qwen3.6:35b"
        assert sess.assistant_ready() is True

    def test_choosing_a_model_does_not_change_anybody_else_s(self, two_people,
                                                             engines,
                                                             monkeypatch):
        import ollama_client

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


# ----------------------------------------------------------------------
# The Qlik connection
# ----------------------------------------------------------------------

class TestEnterpriseGivesEveryoneTheirOwnConnection:

    @pytest.fixture(autouse=True)
    def enterprise(self, monkeypatch):
        monkeypatch.setattr(session, "per_user_engines", lambda: True)

    def test_each_session_connects_as_its_own_user(self, two_people, engines):
        """The whole reason a user has a Qlik account: the engine applies
        that user's own app permissions, and what they build is theirs."""
        alice, bob = two_people
        session.for_user(alice).open_app("sales")
        session.for_user(bob).open_app("sales")

        assert [e.identity for e in engines] == [("BANK", "alice"), ("BANK", "bob")]

    def test_they_are_different_sockets(self, two_people, engines):
        alice, bob = two_people
        assert (session.for_user(alice).open_app("sales")
                is not session.for_user(bob).open_app("sales"))

    def test_they_do_not_wait_on_each_other(self, two_people, engines):
        """Separate sockets mean separate locks, which is what lets two
        people actually work at once."""
        alice, bob = two_people
        assert session.for_user(alice).lock is not session.for_user(bob).lock

    def test_a_user_with_no_qlik_identity_falls_back(self, engines):
        """So the system is demonstrable before every account has been
        matched to a Qlik one."""
        nobody = users.create("carol", "carol-password-1")
        sess = session.for_user(nobody)
        assert sess.impersonates is False
        sess.open_app("sales")
        assert engines[0].identity == (None, None)

    def test_changing_someone_s_qlik_identity_drops_their_connection(
            self, two_people, engines):
        """The socket was opened as who they used to be."""
        alice, _ = two_people
        first = session.for_user(alice)
        first.open_app("sales")

        moved = dict(alice, qlik_user_id="alice.smith")
        again = session.for_user(moved)

        assert again is first
        assert first.state["engine"] is None
        assert engines[0].closed is True


class TestDesktopSharesOneConnection:
    """Qlik Sense Desktop permits one session per app and has no identities
    to impersonate, so a second socket does not give a second user their own
    view - it fails to open the app at all."""

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
        """The failure this prevents is silent: one person's question
        answered against another person's data model, confidently."""
        alice, bob = two_people
        one, other = session.for_user(alice), session.for_user(bob)

        one.open_app("sales")
        other.open_app("risk")          # takes the shared socket to 'risk'

        assert one.engine().app_name == "sales"
        assert other.engine().app_name == "risk"

    def test_the_displaced_user_is_not_told_their_app_closed(self, two_people,
                                                             engines):
        """They did not close anything. Somebody else switched app."""
        alice, bob = two_people
        one, other = session.for_user(alice), session.for_user(bob)

        one.open_app("sales")
        other.open_app("risk")

        assert one.connected() is False       # the socket really did go
        assert one.engine().app_name == "sales"   # and comes straight back
        assert one.app_name() == "sales"

    def test_nothing_open_is_still_an_error(self, two_people, engines):
        """A session that never opened anything is a different thing from one
        whose socket was replaced, and must still say so."""
        from qlik_engine import QlikNotConnectedError

        alice, _ = two_people
        with pytest.raises(QlikNotConnectedError, match="No app is open"):
            session.for_user(alice).engine()


# ----------------------------------------------------------------------
# Over HTTP
# ----------------------------------------------------------------------

class TestOverHttp:

    @pytest.fixture
    def clients(self, two_people, engines):
        """Two browsers, each signed in as a different person."""
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
        """Starting the server opens the configured app for the system
        session, which is nobody's. Without this, everyone's first request
        after signing in said no app was open - which reads as the product
        being broken rather than as a step they had missed."""
        state = clients["alice"].get("/api/state")
        assert state.status_code == 200
        assert state.json()["app"] == "data"

    def test_a_session_that_lost_its_engine_gets_its_own_app_back(
            self, clients, two_people, engines):
        """A lost connection is rebuilt on the app the person was using.

        The distinction this guards has not changed, only the outcome:
        auto-opening the *configured* app is for somebody who has never
        opened anything, and a session that HAS an app must never be
        silently moved to a different one. It is now re-opened on its own
        app instead of being told, on every request until a restart, that
        nothing is open.
        """
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
        """The other half of the same distinction, still true."""
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
        """The id is in the other user's URL bar, so it is guessable by the
        simple method of being told it."""
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
        """It used to be one module-level flag, so one person allowing the
        assistant to reload allowed it for everybody on the server."""
        alice = clients["alice"].post("/api/select", json={"allow_reload": False})
        bob = clients["bob"].post("/api/select", json={})

        assert alice.json()["allow_reload"] is False
        assert bob.json()["allow_reload"] is True

    def test_the_language_is_not_shared(self, clients, engines):
        clients["alice"].post("/api/select", json={"language": "ru"})
        assert clients["bob"].post("/api/select", json={}).json()["language"] == ""


# ----------------------------------------------------------------------
# The command line
# ----------------------------------------------------------------------

class TestTheSystemSession:
    """chat.py, mcp_server.py and check_connection.py are single-user by
    nature and go on calling the module-level functions."""

    def test_the_module_functions_are_the_system_session(self, engines):
        assert session._state is session.system().state

    def test_it_is_not_any_user_s_session(self, two_people, engines):
        alice, _ = two_people
        assert session.for_user(alice) is not session.system()

    def test_it_files_conversations_where_it_always_did(self, engines):
        assert session.system().owner is None
