import pytest

import session
import users


@pytest.fixture
def enterprise(monkeypatch):
    monkeypatch.setattr(session, "QLIK_MODE", session.ENTERPRISE)
    monkeypatch.setattr(session, "AUTH_ENABLED", True)
    monkeypatch.setattr(session, "ALLOW_SHARED_QLIK_IDENTITY", False)
    monkeypatch.setattr(session, "QLIK_USER_DIRECTORY", "QS")
    monkeypatch.setattr(session, "QLIK_USER_ID", "qp20")


def person(**extra):
    row = {"id": 7, "username": "aigerim", "qlik_directory": "",
           "qlik_user_id": ""}
    row.update(extra)
    return row


class TestASignedInUserNeedsTheirOwnQlikIdentity:
    def test_one_without_an_identity_is_refused(self, enterprise):
        sess = session.Session("s1", person())

        with pytest.raises(session.QlikIdentityMissing, match="no Qlik identity"):
            sess._new_engine()

    def test_the_message_names_the_account_and_the_service_account(self,
                                                                   enterprise):
        sess = session.Session("s1", person())

        with pytest.raises(session.QlikIdentityMissing) as caught:
            sess._new_engine()

        message = str(caught.value)
        assert "aigerim" in message
        assert r"QS\qp20" in message
        assert "admin page" in message

    def test_an_admin_is_not_exempt(self, enterprise):
        sess = session.Session("s1", person(username="qp20", id=1))

        with pytest.raises(session.QlikIdentityMissing):
            sess._new_engine()

    def test_one_with_an_identity_impersonates(self, enterprise, monkeypatch):
        made = {}

        def fake(user_directory=None, user_id=None, **kw):
            made["as"] = (user_directory, user_id)
            return object()

        monkeypatch.setattr(session, "QlikEngine", fake)
        sess = session.Session("s1", person(qlik_directory="QS",
                                            qlik_user_id="aigerim"))

        sess._new_engine()
        assert made["as"] == ("QS", "aigerim")

    def test_half_an_identity_is_not_enough(self, enterprise):
        for half in ({"qlik_directory": "QS"}, {"qlik_user_id": "aigerim"}):
            sess = session.Session("s1", person(**half))
            with pytest.raises(session.QlikIdentityMissing):
                sess._new_engine()


class TestWhereTheGuardMustNotApply:
    def test_the_system_session_still_uses_the_service_account(self, enterprise,
                                                               monkeypatch):
        monkeypatch.setattr(session, "QlikEngine", lambda **kw: object())
        sess = session.Session(session.SYSTEM_KEY)

        assert sess.borrows_service_account is False
        assert sess._new_engine() is not None

    def test_desktop_mode_is_untouched(self, monkeypatch):
        monkeypatch.setattr(session, "QLIK_MODE", "desktop")
        monkeypatch.setattr(session, "AUTH_ENABLED", True)
        monkeypatch.setattr(session, "QlikEngine", lambda **kw: object())
        sess = session.Session("s1", person())

        assert sess.borrows_service_account is False
        assert sess._new_engine() is not None

    def test_it_can_be_switched_off_deliberately(self, enterprise, monkeypatch):
        monkeypatch.setattr(session, "ALLOW_SHARED_QLIK_IDENTITY", True)
        monkeypatch.setattr(session, "QlikEngine", lambda **kw: object())
        sess = session.Session("s1", person())

        assert sess.borrows_service_account is False
        assert sess._new_engine() is not None

    def test_with_sign_in_off_there_is_nobody_to_separate(self, enterprise,
                                                          monkeypatch):
        monkeypatch.setattr(session, "AUTH_ENABLED", False)
        monkeypatch.setattr(session, "QlikEngine", lambda **kw: object())
        sess = session.Session("s1", person())

        assert sess.borrows_service_account is False


class TestAnAccountCannotBeCreatedWithoutOne:
    def empty_the_database(self):
        connection = users.connect()
        connection.execute("DELETE FROM users")
        connection.commit()
        assert users.count() == 0

    def enterprise(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "QLIK_MODE", config.ENTERPRISE)
        monkeypatch.setattr(config, "AUTH_ENABLED", True)
        monkeypatch.setattr(config, "ALLOW_SHARED_QLIK_IDENTITY", False)
        monkeypatch.setattr(config, "QLIK_USER_DIRECTORY", "QS")
        monkeypatch.setattr(config, "QLIK_USER_ID", "qp20")

    def test_a_blank_identity_is_refused_at_creation(self, isolated_accounts,
                                                     monkeypatch):
        self.enterprise(monkeypatch)

        with pytest.raises(users.UserError, match="own Qlik identity"):
            users.create("aigerim", "a-good-password")

    def test_half_an_identity_is_refused_too(self, isolated_accounts,
                                             monkeypatch):
        self.enterprise(monkeypatch)

        with pytest.raises(users.UserError, match="own Qlik identity"):
            users.create("aigerim", "a-good-password", qlik_directory="QS")

    def test_a_complete_identity_is_accepted(self, isolated_accounts,
                                             monkeypatch):
        self.enterprise(monkeypatch)

        row = users.create("aigerim", "a-good-password",
                           qlik_directory="QS", qlik_user_id="aigerim")
        assert (row["qlik_directory"], row["qlik_user_id"]) == ("QS", "aigerim")

    def test_the_first_administrator_inherits_it_from_the_env(
            self, isolated_accounts, monkeypatch):
        self.enterprise(monkeypatch)
        monkeypatch.setattr(users, "ADMIN_USERNAME", "qp20")
        monkeypatch.setattr(users, "ADMIN_PASSWORD", "1")
        monkeypatch.setattr(users, "PASSWORD_MIN", 1)
        self.empty_the_database()

        users.bootstrap()

        row = users.by_username("qp20")
        assert (row["qlik_directory"], row["qlik_user_id"]) == ("QS", "qp20")

    def test_bootstrap_says_so_when_the_env_has_nothing_to_give(
            self, isolated_accounts, monkeypatch):
        self.enterprise(monkeypatch)
        monkeypatch.setattr("config.QLIK_USER_ID", "")
        self.empty_the_database()

        with pytest.raises(users.UserError, match="QLIK_USER_ID"):
            users.bootstrap()

    def test_desktop_does_not_ask_for_one(self, isolated_accounts,
                                          monkeypatch):
        import config
        monkeypatch.setattr(config, "QLIK_MODE", "desktop")

        assert users.create("aigerim", "a-good-password")["username"] == "aigerim"


class TestWhereSomebodyLandsOnTheirFirstRequest:
    def engine_that_allows(self, monkeypatch, *allowed):
        from qlik_engine import QlikEngineError

        opened = []

        class Fake:
            mode = "enterprise"

            def __init__(self, **kw):
                self.connected = True
                self.app_name = None

            def open_app(self, name):
                if name not in allowed:
                    raise QlikEngineError(f"Access denied to {name!r}")
                self.app_name = name
                opened.append(name)
                return 1

            def list_apps(self):
                return [{"name": a} for a in allowed]

            def close(self):
                self.connected = False

        monkeypatch.setattr(session, "QlikEngine", Fake)
        return opened

    def test_the_configured_app_is_used_when_it_can_be_opened(self,
                                                              monkeypatch):
        monkeypatch.setattr(session, "APP_NAME", "nerez")
        opened = self.engine_that_allows(monkeypatch, "nerez", "Reports")

        sess = session.Session("s1")
        sess.open_default()

        assert opened == ["nerez"]
        assert sess.app_name() == "nerez"

    def test_somebody_without_it_lands_in_one_they_do_have(self, monkeypatch):
        monkeypatch.setattr(session, "APP_NAME", "nerez")
        self.engine_that_allows(monkeypatch, "Reports")

        sess = session.Session("s1")
        sess.open_default()

        assert sess.app_name() == "Reports", (
            "landing on an app they cannot open leaves them with an error and "
            "nothing to do"
        )

    def test_somebody_with_no_apps_at_all_still_gets_the_real_reason(
            self, monkeypatch):
        from qlik_engine import QlikEngineError

        monkeypatch.setattr(session, "APP_NAME", "nerez")
        self.engine_that_allows(monkeypatch)

        sess = session.Session("s1")
        with pytest.raises(QlikEngineError, match="nerez"):
            sess.open_default()

    def test_a_missing_identity_is_not_swallowed_by_the_fallback(
            self, monkeypatch):
        monkeypatch.setattr(session, "QLIK_MODE", session.ENTERPRISE)
        monkeypatch.setattr(session, "AUTH_ENABLED", True)
        monkeypatch.setattr(session, "ALLOW_SHARED_QLIK_IDENTITY", False)
        monkeypatch.setattr(session, "APP_NAME", "nerez")

        sess = session.Session("s1", {"id": 7, "username": "aigerim",
                                      "qlik_directory": "", "qlik_user_id": ""})

        with pytest.raises(session.QlikIdentityMissing):
            sess.open_default()
