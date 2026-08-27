import pytest
from fastapi.testclient import TestClient

import history
import session
import users
import web_app

pytestmark = pytest.mark.live_auth


class FakeEngine:
    mode = "desktop"

    def __init__(self):
        self.connected = True
        self.app_name = None

    def open_app(self, name):
        self.app_name = name
        return 1

    def close(self):
        self.connected = False

    def list_apps(self):
        return [{"name": "sales"}]

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


@pytest.fixture(autouse=True)
def fake_qlik(monkeypatch):
    monkeypatch.setattr(session, "QlikEngine", lambda *a, **k: FakeEngine())


@pytest.fixture
def boss():
    return users.create("boss", "boss-password-1", role=users.ADMIN,
                        display_name="The Boss")


@pytest.fixture
def member():
    return users.create("jsmith", "jsmith-password-1", display_name="J Smith")


@pytest.fixture
def console(boss):
    client = TestClient(web_app.app)
    assert client.post("/api/login", json={
        "username": "boss", "password": "boss-password-1"}).status_code == 200
    return client


def wrote(action):
    return [e for e in users.audit_trail(limit=500) if e["action"] == action]


class TestManagingUsers:

    def test_the_listing_never_carries_a_password(self, console, member):
        body = console.get("/api/admin/users").json()
        assert {u["username"] for u in body["users"]} == {"boss", "jsmith"}
        assert "password" not in str(body)

    def test_creating_a_user(self, console):
        response = console.post("/api/admin/users", json={
            "username": "bcooper", "password": "bcooper-password-1",
            "display_name": "B Cooper", "qlik_directory": "BANK",
            "qlik_user_id": "bcooper",
        })
        assert response.status_code == 200

        made = users.by_username("bcooper")
        assert made["qlik_user_id"] == "bcooper"
        assert users.authenticate("bcooper", "bcooper-password-1") is not None
        assert wrote("user.created")

    def test_a_bad_username_is_reported_rather_than_crashing(self, console):
        response = console.post("/api/admin/users", json={
            "username": "not a username", "password": "a-good-password"})
        assert response.status_code == 400
        assert "username" in response.json()["detail"].lower()

    def test_a_duplicate_username_is_reported(self, console, member):
        response = console.post("/api/admin/users", json={
            "username": "jsmith", "password": "a-good-password"})
        assert response.status_code == 400

    def test_editing_a_user(self, console, member):
        response = console.patch(f"/api/admin/users/{member['id']}", json={
            "display_name": "Jane Smith", "qlik_directory": "BANK",
            "qlik_user_id": "jane.smith",
        })
        assert response.status_code == 200
        assert users.get(member["id"])["qlik_user_id"] == "jane.smith"
        assert wrote("user.updated")

    def test_disabling_a_user(self, console, member):
        assert console.patch(f"/api/admin/users/{member['id']}",
                             json={"active": False}).status_code == 200
        assert users.authenticate("jsmith", "jsmith-password-1") is None

    def test_resetting_a_password_signs_them_out_everywhere(self, console, member):
        theirs = TestClient(web_app.app)
        theirs.post("/api/login", json={"username": "jsmith",
                                        "password": "jsmith-password-1"})
        assert theirs.get("/api/me").status_code == 200

        console.post(f"/api/admin/users/{member['id']}/password",
                     json={"password": "a-brand-new-password"})

        assert theirs.get("/api/me").status_code == 401
        assert users.authenticate("jsmith", "a-brand-new-password") is not None
        assert wrote("password.reset")

    def test_deleting_a_user(self, console, member):
        assert console.delete(f"/api/admin/users/{member['id']}").status_code == 200
        assert users.by_username("jsmith") is None
        assert wrote("user.deleted")

    def test_you_cannot_delete_the_account_you_are_using(self, console, boss):
        response = console.delete(f"/api/admin/users/{boss['id']}")
        assert response.status_code == 400
        assert "signed in as" in response.json()["detail"]

    def test_the_last_administrator_is_protected(self, console, boss):
        response = console.patch(f"/api/admin/users/{boss['id']}",
                                 json={"role": "user"})
        assert response.status_code == 400
        assert "only active administrator" in response.json()["detail"]

    def test_deleting_a_user_keeps_their_conversations(self, console, member):
        sess = session.for_user(member)
        sess.reset_chat()
        sess.messages().append({"role": "user", "content": "what is our exposure?"})
        sess.persist()
        owner = history.owner_key(member["id"])

        console.delete(f"/api/admin/users/{member['id']}")
        assert history.listing(owner)

    def test_editing_someone_drops_the_session_holding_their_old_identity(
            self, console, member):
        sess = session.for_user(member)
        sess.open_app("sales")
        assert sess.connected() is True

        console.patch(f"/api/admin/users/{member['id']}",
                      json={"qlik_user_id": "someone.else"})

        assert session.key_for(member) not in session._sessions


class TestTheConsoleShowsWhereAPasswordLives:
    def test_the_listing_says_which_accounts_come_from_the_directory(
            self, console, member):
        users.from_directory({"username": "bcooper", "display_name": "B Cooper",
                              "groups": []}, ("BANK", "bcooper"))

        people = {u["username"]: u for u in
                  console.get("/api/admin/users").json()["users"]}
        assert people["bcooper"]["from_directory"] is True
        assert people["jsmith"]["from_directory"] is False

    def test_the_page_is_told_whether_directory_sign_in_is_on(self, console,
                                                              monkeypatch):
        assert console.get("/api/admin/users").json()["directory_sign_in"] is False

        monkeypatch.setattr(web_app.directory, "enabled", lambda: True)
        assert console.get("/api/admin/users").json()["directory_sign_in"] is True

    def test_a_directory_account_has_no_local_password_to_reset(self, console):
        user = users.from_directory({"username": "bcooper", "display_name": "B",
                                     "groups": []}, ("BANK", "bcooper"))
        assert users.authenticate("bcooper", "") is None
        assert user["from_directory"] is True


class TestMcpTokens:

    def test_a_token_is_shown_once(self, console, member):
        response = console.post(f"/api/admin/users/{member['id']}/token")
        assert response.status_code == 200
        token = response.json()["token"]
        assert users.by_token(token)["username"] == "jsmith"

        listed = [u for u in console.get("/api/admin/users").json()["users"]
                  if u["username"] == "jsmith"][0]
        assert listed["has_token"] is True
        assert token not in str(listed)

    def test_it_can_be_revoked(self, console, member):
        token = console.post(f"/api/admin/users/{member['id']}/token").json()["token"]
        console.delete(f"/api/admin/users/{member['id']}/token")
        assert users.by_token(token) is None


class TestReadingConversations:

    @pytest.fixture
    def a_conversation(self, member):
        sess = session.for_user(member)
        sess.reset_chat()
        sess.messages().append({"role": "user", "content": "what is our exposure?"})
        sess.messages().append({"role": "assistant", "content": "About 4.2bn."})
        sess.persist()
        return sess.chat_id()

    def test_everyone_s_chats_are_listed_with_a_name_against_them(
            self, console, member, a_conversation):
        chats = console.get("/api/admin/chats").json()["chats"]
        assert len(chats) == 1
        assert chats[0]["username"] == "jsmith"
        assert chats[0]["display_name"] == "J Smith"
        assert chats[0]["title"] == "what is our exposure?"

    def test_they_can_be_narrowed_to_one_person(self, console, member,
                                                a_conversation, boss):
        mine = session.for_user(boss)
        mine.reset_chat()
        mine.messages().append({"role": "user", "content": "the boss asked this"})
        mine.persist()

        assert len(console.get("/api/admin/chats").json()["chats"]) == 2
        narrowed = console.get(
            f"/api/admin/chats?user_id={member['id']}").json()["chats"]
        assert [c["title"] for c in narrowed] == ["what is our exposure?"]

    def test_a_conversation_can_be_read(self, console, member, a_conversation):
        owner = history.owner_key(member["id"])
        body = console.get(f"/api/admin/chats/{owner}/{a_conversation}").json()
        assert [m["content"] for m in body["transcript"]] == [
            "what is our exposure?", "About 4.2bn."]

    def test_reading_one_is_itself_recorded(self, console, member, a_conversation):
        owner = history.owner_key(member["id"])
        console.get(f"/api/admin/chats/{owner}/{a_conversation}")

        entry = wrote("chat.read")[0]
        assert entry["username"] == "boss"
        assert owner in entry["detail"]

    @pytest.mark.parametrize("owner", [
        "../../etc", "..", "system", "u", "u9999999999999999", "u1;drop",
    ])
    def test_an_owner_that_is_not_an_owner_is_refused(self, console, owner,
                                                      a_conversation):
        response = console.get(f"/api/admin/chats/{owner}/{a_conversation}")
        assert response.status_code == 404

    def test_a_chat_id_that_is_not_a_chat_id_is_refused(self, console, member):
        owner = history.owner_key(member["id"])
        assert console.get(
            f"/api/admin/chats/{owner}/../../secrets").status_code in (404, 400)


class TestSessions:

    def test_it_lists_who_is_signed_in(self, console, member):
        theirs = TestClient(web_app.app)
        theirs.post("/api/login", json={"username": "jsmith",
                                        "password": "jsmith-password-1"})

        listed = console.get("/api/admin/sessions").json()["sessions"]
        assert {s["username"] for s in listed} == {"boss", "jsmith"}

    def test_a_session_can_be_ended(self, console, member):
        theirs = TestClient(web_app.app)
        theirs.post("/api/login", json={"username": "jsmith",
                                        "password": "jsmith-password-1"})

        handle = [s for s in console.get("/api/admin/sessions").json()["sessions"]
                  if s["username"] == "jsmith"][0]["handle"]
        assert console.delete(f"/api/admin/sessions/{handle}").status_code == 200
        assert theirs.get("/api/me").status_code == 401

    def test_it_reports_the_live_qlik_connections(self, console, member):
        session.for_user(member).open_app("sales")
        qlik = console.get("/api/admin/sessions").json()["qlik"]
        assert any(entry["username"] == "jsmith" and entry["app"] == "sales"
                   for entry in qlik)


class TestActingAsAnotherUser:

    def test_it_puts_you_in_their_conversation(self, console, member):
        sess = session.for_user(member)
        sess.reset_chat()
        sess.messages().append({"role": "user", "content": "their question"})
        sess.persist()

        console.post("/api/admin/act-as", json={"user_id": member["id"]})

        chats = console.get("/api/chats").json()["chats"]
        assert [c["title"] for c in chats] == ["their question"]

    def test_the_page_is_told_it_is_borrowed(self, console, member):
        console.post("/api/admin/act-as", json={"user_id": member["id"]})
        me = console.get("/api/me").json()
        assert me["username"] == "jsmith"
        assert me["impersonating"] is True
        assert me["real_username"] == "boss"

    def test_administration_is_refused_while_acting_as_somebody(self, console,
                                                                member):
        console.post("/api/admin/act-as", json={"user_id": member["id"]})

        response = console.get("/api/admin/users")
        assert response.status_code == 403
        assert "acting as another user" in response.json()["detail"].lower()

    def test_it_can_be_stopped(self, console, member):
        console.post("/api/admin/act-as", json={"user_id": member["id"]})
        console.post("/api/admin/act-as", json={"user_id": None})

        assert console.get("/api/me").json()["username"] == "boss"
        assert console.get("/api/admin/users").status_code == 200

    def test_both_ends_are_recorded_and_name_the_real_person(self, console,
                                                             member):
        console.post("/api/admin/act-as", json={"user_id": member["id"]})
        console.post("/api/admin/act-as", json={"user_id": None})

        assert "jsmith" in wrote("act-as.started")[0]["detail"]
        assert wrote("act-as.started")[0]["username"] == "boss"
        assert wrote("act-as.stopped")

    def test_work_done_while_borrowed_names_both(self, console, member):
        console.post("/api/admin/act-as", json={"user_id": member["id"]})
        console.post("/api/select", json={"app": "sales"})

        entry = wrote("app.opened")[0]
        assert entry["username"] == "jsmith"
        assert "boss" in entry["detail"]

    def test_you_cannot_act_as_yourself(self, console, boss):
        response = console.post("/api/admin/act-as", json={"user_id": boss["id"]})
        assert response.status_code == 400

    def test_you_cannot_act_as_a_disabled_user(self, console, member):
        users.update(member["id"], active=False)
        response = console.post("/api/admin/act-as", json={"user_id": member["id"]})
        assert response.status_code == 404

    def test_an_ordinary_user_cannot_act_as_anybody(self, member, boss):
        theirs = TestClient(web_app.app)
        theirs.post("/api/login", json={"username": "jsmith",
                                        "password": "jsmith-password-1"})
        assert theirs.post("/api/admin/act-as",
                           json={"user_id": boss["id"]}).status_code == 403

    def test_stopping_is_reachable_from_inside_a_borrowed_identity(
            self, console, member):
        console.post("/api/admin/act-as", json={"user_id": member["id"]})
        assert console.get("/api/admin/users").status_code == 403

        assert console.post("/api/admin/act-as",
                            json={"user_id": None}).status_code == 200


class TestAuditTrail:

    def test_a_login_is_recorded(self, console):
        assert wrote("login")[0]["username"] == "boss"

    def test_a_failed_login_is_recorded(self, console, member):
        TestClient(web_app.app).post(
            "/api/login", json={"username": "jsmith", "password": "wrong"})
        assert "jsmith" in wrote("login.failed")[0]["detail"]

    def test_rewriting_the_load_script_is_recorded(self, console, boss):
        session.for_user(boss).open_app("sales")
        console.put("/api/script", json={"content": "///$tab Main\r\nTRACE x;\r\n"})
        assert wrote("script.saved")[0]["username"] == "boss"

    def test_reloading_the_data_is_recorded(self, console, boss):
        session.for_user(boss).open_app("sales")
        console.post("/api/reload")
        entry = wrote("data.reloaded")[0]
        assert entry["username"] == "boss"
        assert "sales" in entry["detail"]

    def test_the_trail_can_be_filtered(self, console, member):
        TestClient(web_app.app).post(
            "/api/login", json={"username": "jsmith", "password": "wrong"})

        body = console.get("/api/admin/audit?action=login.failed").json()
        assert [e["action"] for e in body["entries"]] == ["login.failed"]
        assert "login" in body["actions"]
