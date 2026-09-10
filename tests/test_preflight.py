import pytest

import config
import preflight
import session
import users


@pytest.fixture
def enterprise(monkeypatch):
    monkeypatch.setattr(config, "QLIK_MODE", config.ENTERPRISE)
    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    monkeypatch.setattr(config, "ALLOW_SHARED_QLIK_IDENTITY", False)
    monkeypatch.setattr(config, "QLIK_USER_DIRECTORY", "QS")
    monkeypatch.setattr(config, "QLIK_USER_ID", "qp20")
    monkeypatch.setattr(session, "QLIK_MODE", config.ENTERPRISE)


def text(items):
    return preflight.render(items)


class TestItNoticesTheThingsThatActuallyWentWrong:
    def test_desktop_mode_with_sign_in_is_called_out(self, isolated_accounts,
                                                     monkeypatch, enterprise):
        monkeypatch.setattr(config, "QLIK_MODE", "desktop")
        monkeypatch.setattr(session, "QLIK_MODE", "desktop")

        found = preflight.findings()

        assert preflight.worst(found) == preflight.WARN
        body = text(found)
        assert "shares ONE" in body
        assert "QLIK_MODE=enterprise" in body
        assert r"QS\qp20" in body

    def test_sign_in_off_is_called_out_first(self, isolated_accounts,
                                             monkeypatch, enterprise):
        monkeypatch.setattr(config, "AUTH_ENABLED", False)

        found = preflight.findings()

        assert preflight.worst(found) == preflight.WARN
        assert "Sign-in is off" in text(found)

    def test_an_account_with_no_identity_is_named(self, isolated_accounts,
                                                  monkeypatch, enterprise):
        monkeypatch.setattr(config, "ALLOW_SHARED_QLIK_IDENTITY", True)
        users.create("aigerim", "a-good-password")

        found = preflight.findings()
        body = text(found)

        assert "aigerim" in body
        assert "manage_users.py qlik aigerim" in body

    def test_the_deliberate_override_is_called_out(self, isolated_accounts,
                                                   monkeypatch, enterprise):
        monkeypatch.setattr(config, "ALLOW_SHARED_QLIK_IDENTITY", True)

        assert "ALLOW_SHARED_QLIK_IDENTITY" in text(preflight.findings())

    def test_a_correct_setup_says_so(self, isolated_accounts, monkeypatch,
                                     enterprise):
        db = users.connect()
        db.execute("UPDATE users SET qlik_directory=?, qlik_user_id=?",
                   ("QS", "tester"))
        db.commit()

        found = preflight.findings()

        assert preflight.worst(found) == preflight.OK
        assert "their own Qlik identity" in text(found)

    def test_it_never_raises_even_with_no_database(self, monkeypatch,
                                                  enterprise):
        monkeypatch.setattr(users, "listing",
                            lambda: (_ for _ in ()).throw(RuntimeError("no")))

        assert isinstance(preflight.findings(), list)
