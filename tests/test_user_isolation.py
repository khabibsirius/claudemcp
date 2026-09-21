"""Does each signed-in person really reach Qlik as themselves?

These run the whole chain - account, session, engine, websocket - against
`fake_engine`, which hands out apps according to the `X-Qlik-User` header it
was given. No licensed Qlik server is needed, and the failures they catch are
the ones that were actually happening: a shared connection, a borrowed
identity, a header that never arrives.
"""

import pytest

import session
import users
from qlik_engine import QlikEngineError

import fake_engine


AIGERIM = {"id": 11, "username": "aigerim",
           "qlik_directory": "QS", "qlik_user_id": "aigerim"}
BEKZAT = {"id": 12, "username": "bekzat",
          "qlik_directory": "QS", "qlik_user_id": "bekzat"}
NOBODY = {"id": 13, "username": "forgot",
          "qlik_directory": "", "qlik_user_id": ""}


def a_world():
    return fake_engine.World(
        {
            "QS\\aigerim": ["Sales KPI", "Aigerim scratch"],
            "QS\\bekzat": ["Logistics", "Bekzat scratch"],
            "QS\\svc": ["Sales KPI", "Logistics", "Aigerim scratch",
                        "Bekzat scratch", "Monitoring"],
        },
        service_account="QS\\svc",
    )


@pytest.fixture
def enterprise(tmp_path, monkeypatch):
    """Enterprise mode, pointed at the fake engine."""
    certificates = tmp_path / "certs"
    fake_engine.enterprise_engines(monkeypatch, certificates)
    return fake_engine.install(monkeypatch, a_world(),
                               certificates_at=certificates)


@pytest.fixture
def deaf_enterprise(tmp_path, monkeypatch):
    """Enterprise mode against a server that ignores X-Qlik-User."""
    certificates = tmp_path / "certs"
    fake_engine.enterprise_engines(monkeypatch, certificates)
    return fake_engine.install(monkeypatch, a_world(),
                               certificates_at=certificates,
                               header_ignored="QS\\svc")


class TestEachPersonConnectsAsThemselves:
    def test_two_people_open_two_connections(self, enterprise):
        session.Session("u11", AIGERIM).list_apps()
        session.Session("u12", BEKZAT).list_apps()

        assert enterprise.identities == ["QS\\aigerim", "QS\\bekzat"]

    def test_each_connection_carries_its_own_identity_header(self, enterprise):
        session.Session("u11", AIGERIM).list_apps()

        header = enterprise.sockets[0].opened_with["header"]
        assert header == ["X-Qlik-User: UserDirectory=QS; UserId=aigerim"]

    def test_the_connection_is_wss_on_the_engine_port(self, enterprise):
        session.Session("u11", AIGERIM).list_apps()

        assert enterprise.sockets[0].opened_with["url"] == "wss://qlik.test:4747/app/"

    def test_nobody_reaches_qlik_without_an_identity(self, enterprise):
        sess = session.Session("u13", NOBODY)

        with pytest.raises(session.QlikIdentityMissing):
            sess.list_apps()

        assert enterprise.sockets == []


class TestPeopleSeeOnlyTheirOwnApps:
    def test_each_person_gets_their_own_list(self, enterprise):
        mine = session.Session("u11", AIGERIM).list_apps()
        theirs = session.Session("u12", BEKZAT).list_apps()

        assert mine == ["Sales KPI", "Aigerim scratch"]
        assert theirs == ["Logistics", "Bekzat scratch"]

    def test_the_two_lists_do_not_overlap(self, enterprise):
        mine = set(session.Session("u11", AIGERIM).list_apps())
        theirs = set(session.Session("u12", BEKZAT).list_apps())

        assert mine & theirs == set()

    def test_one_person_cannot_open_anothers_app(self, enterprise):
        sess = session.Session("u11", AIGERIM)

        with pytest.raises(QlikEngineError):
            sess.open_app("Logistics")

    def test_a_person_can_open_their_own_app(self, enterprise):
        sess = session.Session("u11", AIGERIM)

        sess.open_app("Sales KPI")

        assert sess.app_name() == "Sales KPI"

    def test_an_identity_qlik_does_not_know_sees_nothing(self, enterprise):
        stranger = {"id": 14, "username": "ghost",
                    "qlik_directory": "QS", "qlik_user_id": "ghost"}

        assert session.Session("u14", stranger).list_apps() == []


class TestTheEngineIsAskedWhoItThinksYouAre:
    def test_whoami_reports_the_impersonated_person(self, enterprise):
        sess = session.Session("u11", AIGERIM)
        sess.open_app("Sales KPI")

        assert sess.state["engine"].whoami() == "UserDirectory=QS; UserId=aigerim"

    def test_a_good_connection_confirms_its_identity(self, enterprise):
        sess = session.Session("u11", AIGERIM)
        sess.open_app("Sales KPI")

        ok, reported = sess.state["engine"].identity_matches()

        assert ok is True
        assert "aigerim" in reported

    def test_an_ignored_header_is_caught(self, deaf_enterprise):
        """The leak that started all this: everyone silently becomes the
        service account. identity_matches() has to notice."""
        sess = session.Session("u11", AIGERIM)
        sess.open_app("Sales KPI")

        ok, reported = sess.state["engine"].identity_matches()

        assert ok is False
        assert "svc" in reported

    def test_an_ignored_header_shows_up_as_everyone_seeing_the_same_apps(
            self, deaf_enterprise):
        mine = session.Session("u11", AIGERIM).list_apps()
        theirs = session.Session("u12", BEKZAT).list_apps()

        assert mine == theirs


class TestNobodySharesAnotherPersonsConnection:
    def test_each_person_gets_their_own_socket(self, enterprise):
        first = session.Session("u11", AIGERIM)
        second = session.Session("u12", BEKZAT)
        first.open_app("Sales KPI")
        second.open_app("Logistics")

        assert first.state["engine"] is not second.state["engine"]

    def test_impersonating_sessions_never_touch_the_shared_engine(self,
                                                                  enterprise):
        first = session.Session("u11", AIGERIM)
        first.open_app("Sales KPI")

        assert first.impersonates is True
        assert first.shares_engine is False

    def test_one_person_switching_apps_leaves_the_other_alone(self, enterprise):
        first = session.Session("u11", AIGERIM)
        second = session.Session("u12", BEKZAT)
        first.open_app("Sales KPI")
        second.open_app("Logistics")

        first.open_app("Aigerim scratch")

        assert first.app_name() == "Aigerim scratch"
        assert second.app_name() == "Logistics"


class TestTheAccountDatabaseHoldsTheLine:
    """The guards that stop the admin page creating a leak in the first
    place, checked against the enterprise rules these tests run under."""

    def enterprise_rules(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "QLIK_MODE", config.ENTERPRISE)
        monkeypatch.setattr(config, "AUTH_ENABLED", True)
        monkeypatch.setattr(config, "ALLOW_SHARED_QLIK_IDENTITY", False)

    def test_two_accounts_cannot_share_one_identity(self, isolated_accounts,
                                                    monkeypatch):
        self.enterprise_rules(monkeypatch)
        users.create("bekzat", "a-good-password",
                     qlik_directory="QS", qlik_user_id="bekzat")

        with pytest.raises(users.UserError, match="already connects to Qlik"):
            users.create("aigerim", "a-good-password",
                         qlik_directory="QS", qlik_user_id="bekzat")

    def test_an_edit_cannot_blank_an_identity(self, isolated_accounts,
                                              monkeypatch):
        self.enterprise_rules(monkeypatch)
        person = users.create("bekzat", "a-good-password",
                              qlik_directory="QS", qlik_user_id="bekzat")

        with pytest.raises(users.UserError, match="own Qlik identity"):
            users.update(person["id"], qlik_user_id="")
