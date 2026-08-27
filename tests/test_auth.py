import pytest
from fastapi.testclient import TestClient

import auth
import session
import users
import web_app

pytestmark = pytest.mark.live_auth


SCRIPT = "///$tab Main\r\nLOAD * FROM [lib://dataset/x.csv];\r\n"


class FakeEngine:
    mode = "desktop"
    connected = True
    app_name = "data"

    def open_app(self, name):
        self.app_name = name
        return 1

    def list_apps(self):
        return [{"name": "data"}]

    def get_script(self):
        return SCRIPT

    def set_script(self, script, validate=True):
        return SCRIPT

    def list_connections(self):
        return []

    def list_sheets(self):
        return []

    def get_fields(self):
        return []

    def get_tables(self):
        return []

    def app_file_info(self):
        return {"path": "C:/apps/data.qvf", "size_bytes": 1, "modified": "now"}

    def reload_data(self):
        return {"success": True, "errors": []}

    def save(self):
        return True

    def close(self):
        self.connected = False


@pytest.fixture
def people():
    return {
        "boss": users.create("boss", "boss-password-1", role=users.ADMIN,
                             display_name="The Boss"),
        "jsmith": users.create("jsmith", "jsmith-password-1",
                               display_name="J Smith"),
    }


@pytest.fixture
def client(people, monkeypatch):
    fake = FakeEngine()
    monkeypatch.setattr(session, "QlikEngine", lambda *a, **k: fake)
    monkeypatch.setattr(
        session.Session, "engine", lambda self: fake)
    return TestClient(web_app.app)


def sign_in(client, username, password):
    return client.post("/api/login",
                       json={"username": username, "password": password})


class TestNothingIsReachableWithoutSigningIn:

    @pytest.mark.parametrize("method,path", [
        ("get", "/api/state"),
        ("get", "/api/options"),
        ("get", "/api/chats"),
        ("get", "/api/me"),
        ("post", "/api/select"),
        ("post", "/api/reload"),
        ("post", "/api/save"),
        ("post", "/api/chat"),
        ("post", "/api/chat/stream"),
        ("put", "/api/script"),
        ("get", "/api/admin/users"),
        ("get", "/api/admin/audit"),
        ("get", "/api/admin/chats"),
    ])
    def test_the_api_answers_401(self, client, method, path):
        call = getattr(client, method)
        response = call(path) if method == "get" else call(path, json={})
        assert response.status_code == 401

    def test_the_mcp_endpoint_is_closed_too(self, client):
        response = client.post("/mcp", json={"jsonrpc": "2.0", "method": "initialize",
                                             "id": 1, "params": {}})
        assert response.status_code == 401

    def test_a_browser_is_sent_to_the_login_page(self, client):
        response = client.get("/", headers={"accept": "text/html"},
                              follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    def test_the_login_page_itself_is_reachable(self, client):
        assert client.get("/login").status_code == 200

    def test_the_health_check_is_reachable(self, client):
        assert client.get("/healthz").status_code == 200

    def test_a_new_endpoint_is_closed_by_forgetting_rather_than_open(self):
        assert auth.is_public("/api/something-added-next-week") is False
        assert auth.is_public("/api/state") is False
        assert auth.is_public("/login") is True


class TestSigningIn:

    def test_the_right_password_opens_the_door(self, client):
        assert sign_in(client, "jsmith", "jsmith-password-1").status_code == 200
        assert client.get("/api/state").status_code == 200

    def test_the_cookie_is_not_readable_from_script(self, client):
        sign_in(client, "jsmith", "jsmith-password-1")
        header = client.cookies.jar._cookies
        raw = str(sign_in(client, "jsmith", "jsmith-password-1").headers)
        assert "httponly" in raw.lower()
        assert "samesite=lax" in raw.lower()
        assert header

    def test_the_wrong_password_is_refused(self, client):
        assert sign_in(client, "jsmith", "not-it").status_code == 401
        assert client.get("/api/state").status_code == 401

    def test_an_unknown_user_and_a_wrong_password_look_identical(self, client):
        missing = sign_in(client, "nobody-at-all", "some-password")
        wrong = sign_in(client, "jsmith", "some-password")
        assert missing.status_code == wrong.status_code == 401
        assert missing.json()["detail"] == wrong.json()["detail"]

    def test_a_locked_account_says_so(self, client, monkeypatch):
        monkeypatch.setattr(users, "LOGIN_MAX_ATTEMPTS", 2)
        sign_in(client, "jsmith", "wrong")
        sign_in(client, "jsmith", "wrong")

        response = sign_in(client, "jsmith", "jsmith-password-1")
        assert response.status_code == 429
        assert "locked" in response.json()["detail"].lower()

    def test_signing_out_ends_the_session(self, client):
        sign_in(client, "jsmith", "jsmith-password-1")
        assert client.post("/api/logout").status_code == 200
        assert client.get("/api/state").status_code == 401

    def test_a_disabled_account_stops_working_mid_session(self, client, people):
        sign_in(client, "jsmith", "jsmith-password-1")
        assert client.get("/api/state").status_code == 200

        users.update(people["jsmith"]["id"], active=False)
        assert client.get("/api/state").status_code == 401

    def test_who_am_i_names_the_person(self, client):
        sign_in(client, "jsmith", "jsmith-password-1")
        me = client.get("/api/me").json()
        assert me["username"] == "jsmith"
        assert me["display_name"] == "J Smith"
        assert me["is_admin"] is False
        assert me["impersonating"] is False

    def test_the_public_session_endpoint_reports_signed_out(self, client):
        assert client.get("/api/session").json()["signed_in"] is False
        sign_in(client, "jsmith", "jsmith-password-1")
        assert client.get("/api/session").json()["signed_in"] is True


class TestChangingYourOwnPassword:

    def test_it_takes_effect(self, client):
        sign_in(client, "jsmith", "jsmith-password-1")
        assert client.post("/api/me/password",
                           json={"password": "a-new-password"}).status_code == 200
        client.post("/api/logout")
        assert sign_in(client, "jsmith", "jsmith-password-1").status_code == 401
        assert sign_in(client, "jsmith", "a-new-password").status_code == 200

    def test_a_short_one_is_refused(self, client):
        sign_in(client, "jsmith", "jsmith-password-1")
        assert client.post("/api/me/password", json={"password": "no"}).status_code == 400


class TestApiTokens:
    def test_a_token_gets_in(self, client, people):
        token = users.mint_token(people["jsmith"]["id"])
        response = client.get("/api/state", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
        assert response.json()["user"]["username"] == "jsmith"

    def test_an_administrator_s_token_opens_the_mcp_endpoint(self, live_app,
                                                              people):
        token = users.mint_token(people["boss"]["id"])
        response = live_app.post(
            "/mcp",
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "method": "initialize", "id": 1,
                  "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                             "clientInfo": {"name": "t", "version": "1"}}},
        )
        assert response.status_code == 200

    def test_a_wrong_token_does_not(self, client):
        response = client.get("/api/state",
                              headers={"Authorization": "Bearer not-a-real-token"})
        assert response.status_code == 401

    def test_a_revoked_token_does_not(self, client, people):
        token = users.mint_token(people["jsmith"]["id"])
        users.revoke_token(people["jsmith"]["id"])
        assert client.get("/api/state",
                          headers={"Authorization": f"Bearer {token}"}).status_code == 401

    def test_a_token_does_not_let_you_act_as_someone_else(self, client, people):
        token = users.mint_token(people["boss"]["id"])
        response = client.post("/api/admin/act-as",
                               headers={"Authorization": f"Bearer {token}"},
                               json={"user_id": people["jsmith"]["id"]})
        assert response.status_code == 400


class TestMcpIsAdministratorsOnly:
    def test_an_ordinary_user_is_refused_with_a_reason(self, client, people):
        token = users.mint_token(people["jsmith"]["id"])
        response = client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {token}"},
            json={"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": {}},
        )
        assert response.status_code == 403
        assert "shared Qlik session" in response.json()["detail"]

    def test_a_signed_in_ordinary_user_is_refused_too(self, client):
        sign_in(client, "jsmith", "jsmith-password-1")
        response = client.post("/mcp", json={"jsonrpc": "2.0", "method": "initialize",
                                             "id": 1, "params": {}})
        assert response.status_code == 403


class TestTheMcpEndpointIsWhereTheDocumentationSays:
    def frames(self, response):
        return [line[6:] for line in response.text.splitlines()
                if line.startswith("data: ")]

    def test_a_real_handshake_completes_at_slash_mcp(self, live_app, people):
        import json

        token = users.mint_token(people["boss"]["id"])
        response = live_app.post(
            "/mcp",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2024-11-05",
                             "capabilities": {},
                             "clientInfo": {"name": "test", "version": "1"}}},
        )

        assert response.status_code == 200
        result = json.loads(self.frames(response)[0])["result"]
        assert result["serverInfo"]["name"] == "qlik-dashboard-builder"
        assert "tools" in result["capabilities"]

    def test_the_doubled_path_is_gone(self, live_app, people):
        token = users.mint_token(people["boss"]["id"])
        response = live_app.post(
            "/mcp/mcp",
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            follow_redirects=False,
        )
        assert response.status_code == 404


class TestAdministratorsOnly:

    @pytest.mark.parametrize("path", [
        "/api/admin/users", "/api/admin/audit", "/api/admin/chats",
        "/api/admin/sessions",
    ])
    def test_an_ordinary_user_is_refused(self, client, path):
        sign_in(client, "jsmith", "jsmith-password-1")
        assert client.get(path).status_code == 403

    def test_an_administrator_is_allowed(self, client):
        sign_in(client, "boss", "boss-password-1")
        assert client.get("/api/admin/users").status_code == 200

    def test_the_admin_page_sends_an_ordinary_user_home(self, client):
        sign_in(client, "jsmith", "jsmith-password-1")
        response = client.get("/admin", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/"

    def test_the_admin_page_opens_for_an_administrator(self, client):
        sign_in(client, "boss", "boss-password-1")
        assert client.get("/admin").status_code == 200


class TestAuthTurnedOff:

    def test_everything_is_open_and_the_operator_is_local(self, client, monkeypatch):
        monkeypatch.setattr(auth, "AUTH_ENABLED", False)
        me = client.get("/api/me")
        assert me.status_code == 200
        assert me.json()["username"] == "local"
        assert me.json()["auth_enabled"] is False

    def test_it_refuses_to_serve_a_real_interface(self, monkeypatch, capsys):
        monkeypatch.setattr(web_app, "AUTH_ENABLED", False)
        assert web_app.main(["--host", "0.0.0.0"]) == 1
        assert "Refusing" in capsys.readouterr().err

    def test_loopback_is_still_allowed(self):
        assert web_app._loopback("127.0.0.1") is True
        assert web_app._loopback("localhost") is True
        assert web_app._loopback("::1") is True
        assert web_app._loopback("0.0.0.0") is False
        assert web_app._loopback("10.0.0.9") is False
