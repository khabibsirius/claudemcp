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
