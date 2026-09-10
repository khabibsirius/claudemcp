import json

import httpx
import pytest

import directory
import qrs
import users


@pytest.fixture
def store(isolated_accounts):
    return None


def person(user_id="aigerim", directory_name="QS", **extra):
    row = {
        "id": "guid-" + user_id,
        "userDirectory": directory_name,
        "userId": user_id,
        "name": user_id.title(),
        "roles": [],
        "inactive": False,
        "removedExternally": False,
    }
    row.update(extra)
    return row


@pytest.fixture
def repository(monkeypatch):
    seen = []
    state = {"people": [], "status": 200, "body": None}

    def handle(request):
        seen.append(request)
        if state["status"] != 200:
            return httpx.Response(state["status"],
                                  json={"message": "no"})
        if state["body"] is not None:
            return httpx.Response(200, content=state["body"])
        query = dict(request.url.params)
        skip = int(query.get("skip", 0))
        take = int(query.get("take", 200))
        return httpx.Response(200, json=state["people"][skip:skip + take])

    monkeypatch.setattr(qrs, "where", lambda: "https://qs:4242/qrs")
    monkeypatch.setattr(
        qrs, "_client",
        lambda: httpx.Client(transport=httpx.MockTransport(handle)))
    state["requests"] = seen
    return state


class TestTheRequestQrsDemands:
    def test_the_xrfkey_is_in_the_header_and_the_query_and_matches(
            self, repository):
        qrs.fetch_users()
        request = repository["requests"][0]

        header = request.headers["X-Qlik-Xrfkey"]
        assert header == dict(request.url.params)["xrfkey"]
        assert len(header) == 16
        assert header.isalnum()

    def test_a_fresh_key_each_time(self, repository):
        qrs.fetch_users()
        qrs.fetch_users()
        keys = {r.headers["X-Qlik-Xrfkey"] for r in repository["requests"]}
        assert len(keys) == len(repository["requests"])

    def test_it_identifies_itself_the_way_qrs_expects(self, repository):
        qrs.fetch_users()
        assert (repository["requests"][0].headers["X-Qlik-User"]
                == "UserDirectory=INTERNAL;UserId=sa_repository"), (
            "Qlik documents this header without a space after the "
            "semicolon; a space has been seen parsed as part of the "
            "user id, which denies every account identically")

    def test_a_forward_slash_is_accepted_too(self, monkeypatch):
        monkeypatch.setattr(qrs, "QRS_AS_USER", "INTERNAL/sa_repository")
        assert qrs._as_user() == "UserDirectory=INTERNAL;UserId=sa_repository"

    def test_a_malformed_as_user_says_so(self, monkeypatch):
        monkeypatch.setattr(qrs, "QRS_AS_USER", "sa_repository")
        with pytest.raises(qrs.QrsError, match="DIRECTORY"):
            qrs._as_user()


class TestWhatComesBack:
    def test_it_pages_until_the_repository_runs_out(self, repository,
                                                    monkeypatch):
        repository["people"] = [person(f"user{i}") for i in range(7)]
        monkeypatch.setattr(qrs, "PAGE", 2)

        rows = qrs.fetch_users()

        assert [r["userId"] for r in rows] == [f"user{i}" for i in range(7)]
        skips = [dict(r.url.params).get("skip")
                 for r in repository["requests"]]
        assert skips == ["0", "2", "4", "6"]

    def test_a_rejected_certificate_is_explained(self, repository):
        repository["status"] = 401
        with pytest.raises(qrs.QrsError, match="rejected the certificate"):
            qrs.fetch_users()

    def test_missing_rights_are_explained(self, repository):
        repository["status"] = 403
        with pytest.raises(qrs.QrsError, match="refused the request"):
            qrs.fetch_users()

    def test_any_other_failure_carries_the_status(self, repository):
        repository["status"] = 500
        with pytest.raises(qrs.QrsError, match="500"):
            qrs.fetch_users()

    def test_something_that_is_not_json_is_reported(self, repository):
        repository["body"] = b"<html>not json</html>"
        with pytest.raises(qrs.QrsError, match="not JSON"):
            qrs.fetch_users()

    def test_a_dict_instead_of_a_list_is_refused(self, repository):
        repository["body"] = json.dumps({"nope": True}).encode()
        with pytest.raises(qrs.QrsError, match="list of users"):
            qrs.fetch_users()


class TestWhoIsLeftOut:
    def test_the_internal_directory_is_skipped(self):
        assert qrs.skipped(person("sa_repository", "INTERNAL"))

    def test_somebody_removed_from_the_directory_is_skipped(self):
        assert qrs.skipped(person(removedExternally=True))

    def test_an_inactive_account_is_skipped(self):
        assert qrs.skipped(person(inactive=True))

    def test_a_blank_user_id_is_skipped(self):
        assert qrs.skipped(person(""))

    def test_an_ordinary_person_is_kept(self):
        assert qrs.skipped(person()) is None


class TestWhoBecomesAnAdministrator:
    def test_by_qlik_role(self, monkeypatch):
        monkeypatch.setattr(qrs, "QRS_ADMIN_PROPERTY", "")
        monkeypatch.setattr(qrs, "QRS_ADMIN_ROLE", "RootAdmin")

        assert qrs.is_admin(person(roles=["RootAdmin"]))
        assert not qrs.is_admin(person(roles=["ContentAdmin"]))
        assert not qrs.is_admin(person())

    def test_the_role_match_ignores_case(self, monkeypatch):
        monkeypatch.setattr(qrs, "QRS_ADMIN_PROPERTY", "")
        monkeypatch.setattr(qrs, "QRS_ADMIN_ROLE", "rootadmin")
        assert qrs.is_admin(person(roles=["RootAdmin"]))

    def test_a_custom_property_wins_over_the_role(self, monkeypatch):
        monkeypatch.setattr(qrs, "QRS_ADMIN_PROPERTY", "AIAccess")
        monkeypatch.setattr(qrs, "QRS_ADMIN_VALUE", "admin")
        monkeypatch.setattr(qrs, "QRS_ADMIN_ROLE", "RootAdmin")

        by_role = person(roles=["RootAdmin"])
        by_property = person(customProperties=[
            {"definition": {"name": "AIAccess"}, "value": "admin"}])

        assert not qrs.is_admin(by_role)
        assert qrs.is_admin(by_property)

    def test_the_wrong_property_value_is_not_enough(self, monkeypatch):
        monkeypatch.setattr(qrs, "QRS_ADMIN_PROPERTY", "AIAccess")
        monkeypatch.setattr(qrs, "QRS_ADMIN_VALUE", "admin")

        assert not qrs.is_admin(person(customProperties=[
            {"definition": {"name": "AIAccess"}, "value": "reader"}]))

    def test_no_marker_configured_means_nobody(self, monkeypatch):
        monkeypatch.setattr(qrs, "QRS_ADMIN_PROPERTY", "")
        monkeypatch.setattr(qrs, "QRS_ADMIN_ROLE", "")
        assert not qrs.is_admin(person(roles=["RootAdmin"]))


class TestSyncing:
    def rows(self):
        return [
            person("qp20", roles=["RootAdmin"]),
            person("aigerim"),
            person("sa_repository", "INTERNAL"),
            person("dana", inactive=True),
        ]

    def test_it_creates_accounts_with_the_right_qlik_identity(self, store,
                                                              monkeypatch):
        monkeypatch.setattr(directory, "enabled", lambda: False)
        monkeypatch.setattr(qrs, "QRS_ADMIN_PROPERTY", "")

        outcome = qrs.sync(self.rows())

        assert sorted(n for n, _, _ in outcome["created"]) == ["aigerim", "qp20"]
        assert len(outcome["skipped"]) == 2

        for name, qlik_id in (("qp20", "qp20"), ("aigerim", "aigerim")):
            row = users.by_username(name)
            assert row["qlik_directory"] == "QS"
            assert row["qlik_user_id"] == qlik_id

    def test_the_qlik_role_decides_who_is_an_administrator(self, store,
                                                           monkeypatch):
        monkeypatch.setattr(directory, "enabled", lambda: False)
        monkeypatch.setattr(qrs, "QRS_ADMIN_PROPERTY", "")

        qrs.sync(self.rows())

        assert users.by_username("qp20")["role"] == users.ADMIN
        assert users.by_username("aigerim")["role"] == users.USER

    def test_running_it_twice_changes_nothing(self, store, monkeypatch):
        monkeypatch.setattr(directory, "enabled", lambda: False)
        qrs.sync(self.rows())

        again = qrs.sync(self.rows())

        assert again["created"] == []
        assert again["updated"] == []
        assert len(again["unchanged"]) == 2

    def test_a_promotion_in_the_qmc_reaches_the_account(self, store,
                                                        monkeypatch):
        monkeypatch.setattr(directory, "enabled", lambda: False)
        monkeypatch.setattr(qrs, "QRS_ADMIN_PROPERTY", "")
        qrs.sync(self.rows())
        assert users.by_username("aigerim")["role"] == users.USER

        promoted = self.rows()
        promoted[1]["roles"] = ["RootAdmin"]
        outcome = qrs.sync(promoted)

        assert [n for n, _, _ in outcome["updated"]] == ["aigerim"]
        assert users.by_username("aigerim")["role"] == users.ADMIN

    def test_it_repairs_an_account_created_without_an_identity(self, store,
                                                              monkeypatch):
        monkeypatch.setattr(directory, "enabled", lambda: False)
        users.create("aigerim", "a-good-password")
        assert users.by_username("aigerim")["qlik_user_id"] == ""

        qrs.sync([person("aigerim")])

        row = users.by_username("aigerim")
        assert (row["qlik_directory"], row["qlik_user_id"]) == ("QS", "aigerim")

    def test_a_qlik_id_that_is_not_a_username_here_is_still_usable(
            self, store, monkeypatch):
        monkeypatch.setattr(directory, "enabled", lambda: False)

        qrs.sync([person("Bekzat.S")])

        row = users.by_username("bekzat.s")
        assert row is not None
        assert row["qlik_user_id"] == "Bekzat.S", (
            "Qlik needs the original casing to impersonate; only the local "
            "username is normalised"
        )

    def test_with_ldap_off_new_accounts_get_a_password(self, store,
                                                       monkeypatch):
        monkeypatch.setattr(directory, "enabled", lambda: False)

        outcome = qrs.sync([person("aigerim")])

        assert set(outcome["passwords"]) == {"aigerim"}
        assert len(outcome["passwords"]["aigerim"]) >= 12
        assert users.by_username("aigerim")["auth_source"] == users.LOCAL

    def test_with_ldap_on_nobody_gets_a_local_password(self, store,
                                                       monkeypatch):
        monkeypatch.setattr(directory, "enabled", lambda: True)

        outcome = qrs.sync([person("aigerim")])

        assert outcome["passwords"] == {}
        assert users.by_username("aigerim")["auth_source"] == users.DIRECTORY

    def test_one_bad_account_does_not_stop_the_others(self, store,
                                                      monkeypatch):
        monkeypatch.setattr(directory, "enabled", lambda: False)

        rows = [person("aigerim"), person("!!!"), person("bekzat")]
        outcome = qrs.sync(rows)

        assert sorted(n for n, _, _ in outcome["created"]) == ["aigerim",
                                                               "bekzat"]
        assert len(outcome["skipped"]) == 1

    def test_it_reads_the_repository_when_given_no_rows(self, store,
                                                        repository,
                                                        monkeypatch):
        monkeypatch.setattr(directory, "enabled", lambda: False)
        repository["people"] = [person("aigerim")]

        outcome = qrs.sync()

        assert [n for n, _, _ in outcome["created"]] == ["aigerim"]


class TestTheSwitch:
    def test_it_is_off_unless_asked_for(self, monkeypatch):
        monkeypatch.setattr(qrs, "QRS_ENABLED", False)
        assert qrs.enabled() is False

    def test_the_summary_says_where_and_as_whom(self, monkeypatch):
        monkeypatch.setattr(qrs, "QRS_ENABLED", True)
        monkeypatch.setattr(qrs, "QRS_ADMIN_PROPERTY", "")
        text = qrs.summary()
        assert "4242" in text
        assert "sa_repository" in text
        assert "RootAdmin" in text


class TestAskingWhatOnePersonCanOpen:
    def repo(self, monkeypatch, by_user):
        seen = []

        def handle(request):
            seen.append(request.headers.get("X-Qlik-User"))
            key = (request.headers.get("X-Qlik-User") or "")
            key = key.replace("UserDirectory=", "").replace(";UserId=", "\\")
            if key not in by_user:
                return httpx.Response(403, json={"message": "no"})
            return httpx.Response(200, json=by_user[key])

        monkeypatch.setattr(qrs, "where", lambda: "https://qs:4242/qrs")
        monkeypatch.setattr(
            qrs, "_client",
            lambda: httpx.Client(transport=httpx.MockTransport(handle)))
        return seen

    def app(self, name, privileges=("read",), stream=None):
        return {
            "id": "guid-" + name, "name": name, "published": bool(stream),
            "stream": {"name": stream} if stream else None,
            "owner": {"userDirectory": "QS", "userId": "qp20"},
            "privileges": list(privileges),
        }

    def test_it_asks_as_the_person_not_the_service_account(self, monkeypatch):
        seen = self.repo(monkeypatch, {"QS\\qp19": [self.app("123")]})

        qrs.hub_apps("QS", "qp19")

        assert seen == ["UserDirectory=QS;UserId=qp19"], (
            "the hub list is per-user; asking as the service account would "
            "return the service account's apps"
        )

    def test_two_people_get_different_answers(self, monkeypatch):
        self.repo(monkeypatch, {
            "QS\\qp20": [self.app("nerez"), self.app("123", stream="Finance")],
            "QS\\qp19": [self.app("123", stream="Finance")],
        })

        boss = [a["name"] for a in qrs.hub_apps("QS", "qp20")]
        other = [a["name"] for a in qrs.hub_apps("QS", "qp19")]

        assert boss == ["123", "nerez"]
        assert other == ["123"]

    def test_privileges_come_back_per_app(self, monkeypatch):
        self.repo(monkeypatch, {"QS\\qp19": [
            self.app("readonly", ("read",)),
            self.app("writable", ("read", "update")),
        ]})

        apps = {a["name"]: a["privileges"] for a in qrs.hub_apps("QS", "qp19")}

        assert apps["readonly"] == ["read"]
        assert apps["writable"] == ["read", "update"]

    def test_somebody_with_nothing_gets_an_empty_list_not_an_error(
            self, monkeypatch):
        self.repo(monkeypatch, {"QS\\nobody": []})

        assert qrs.hub_apps("QS", "nobody") == []

    def test_the_stream_and_owner_are_carried_through(self, monkeypatch):
        self.repo(monkeypatch, {"QS\\qp19": [
            self.app("123", stream="Finance")]})

        app = qrs.hub_apps("QS", "qp19")[0]

        assert app["stream"] == "Finance"
        assert app["owner"] == "QS\\qp20"
        assert app["published"] is True

    def test_a_refusal_is_reported(self, monkeypatch):
        self.repo(monkeypatch, {})

        with pytest.raises(qrs.QrsError):
            qrs.hub_apps("QS", "qp19")
