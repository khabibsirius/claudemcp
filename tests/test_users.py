import pytest

import users

pytestmark = pytest.mark.live_auth


@pytest.fixture
def store(isolated_accounts):
    users.connect()
    return users


class TestPasswords:

    def test_a_password_is_never_stored_in_the_clear(self, store):
        user = users.create("jsmith", "correct horse battery")
        row = users.connect().execute(
            "SELECT password FROM users WHERE id = ?", (user["id"],)).fetchone()
        assert "correct horse battery" not in row["password"]
        assert row["password"].startswith("pbkdf2_sha256$")

    def test_the_same_password_hashes_differently_for_two_people(self, store):
        a = users.hash_password("same-password")
        b = users.hash_password("same-password")
        assert a != b
        assert users.check_password("same-password", a)
        assert users.check_password("same-password", b)

    def test_a_wrong_password_is_refused(self, store):
        stored = users.hash_password("right")
        assert users.check_password("wrong", stored) is False

    def test_a_malformed_hash_is_refused_rather_than_crashing(self, store):
        for rubbish in ("", "nonsense", "pbkdf2_sha256$x$y$z", None, 12):
            assert users.check_password("anything", rubbish) is False

    def test_the_cost_is_read_from_the_value_not_the_setting(self, store,
                                                             monkeypatch):
        stored = users.hash_password("secret", rounds=1200)
        monkeypatch.setattr(users, "PBKDF2_ROUNDS", 99_000)
        assert users.check_password("secret", stored) is True

    def test_a_password_under_the_minimum_is_refused(self, store, monkeypatch):
        monkeypatch.setattr(users, "PASSWORD_MIN", 8)
        with pytest.raises(users.UserError, match="at least"):
            users.create("jsmith", "short")

    def test_an_empty_password_is_refused_however_low_the_minimum(self, store,
                                                                  monkeypatch):
        monkeypatch.setattr(users, "PASSWORD_MIN", 1)
        with pytest.raises(users.UserError, match="at least"):
            users.create("jsmith", "")

    def test_a_one_character_password_is_allowed_when_configured(self, store,
                                                                 monkeypatch):
        monkeypatch.setattr(users, "PASSWORD_MIN", 1)
        assert users.create("jsmith", "a")["username"] == "jsmith"


class TestCreating:

    def test_a_user_is_returned_without_their_password(self, store):
        user = users.create("jsmith", "a-good-password")
        assert "password" not in user
        assert user["username"] == "jsmith"
        assert user["role"] == users.USER
        assert user["is_admin"] is False

    def test_usernames_are_lowercased(self, store):
        assert users.create("JSmith", "a-good-password")["username"] == "jsmith"

    def test_a_duplicate_username_is_refused(self, store):
        users.create("jsmith", "a-good-password")
        with pytest.raises(users.UserError, match="already a user"):
            users.create("jsmith", "another-password")

    @pytest.mark.parametrize("bad", [
        "", "a", "-leading", ".dot", "has space", "has/slash", "../escape",
        "x" * 33, "CAPS ONLY!",
    ])
    def test_a_username_outside_the_alphabet_is_refused(self, store, bad):
        with pytest.raises(users.UserError):
            users.create(bad, "a-good-password")

    def test_an_unknown_role_is_refused(self, store):
        with pytest.raises(users.UserError, match="Role must be"):
            users.create("jsmith", "a-good-password", role="superuser")

    def test_the_qlik_identity_is_kept(self, store):
        user = users.create("jsmith", "a-good-password",
                            qlik_directory="BANK", qlik_user_id="jsmith")
        assert user["qlik_directory"] == "BANK"
        assert user["qlik_user_id"] == "jsmith"


class TestChanging:

    def test_only_listed_fields_can_be_changed(self, store):
        user = users.create("jsmith", "a-good-password")
        with pytest.raises(users.UserError, match="Cannot change"):
            users.update(user["id"], username="someone-else")
        with pytest.raises(users.UserError, match="Cannot change"):
            users.update(user["id"], password="hunter2")

    def test_promoting_and_demoting(self, store):
        users.create("boss", "a-good-password", role=users.ADMIN)
        user = users.create("jsmith", "a-good-password")
        assert users.update(user["id"], role=users.ADMIN)["is_admin"] is True
        assert users.update(user["id"], role=users.USER)["is_admin"] is False

    def test_the_last_administrator_cannot_be_demoted(self, store):
        boss = users.create("boss", "a-good-password", role=users.ADMIN)
        with pytest.raises(users.UserError, match="only active administrator"):
            users.update(boss["id"], role=users.USER)

    def test_the_last_administrator_cannot_be_disabled(self, store):
        boss = users.create("boss", "a-good-password", role=users.ADMIN)
        with pytest.raises(users.UserError, match="only active administrator"):
            users.update(boss["id"], active=False)

    def test_the_last_administrator_cannot_be_deleted(self, store):
        boss = users.create("boss", "a-good-password", role=users.ADMIN)
        with pytest.raises(users.UserError, match="only active administrator"):
            users.delete(boss["id"])

    def test_one_of_two_administrators_can_be_demoted(self, store):
        users.create("boss", "a-good-password", role=users.ADMIN)
        deputy = users.create("deputy", "a-good-password", role=users.ADMIN)
        assert users.update(deputy["id"], role=users.USER)["is_admin"] is False

    def test_disabling_ends_their_sessions_immediately(self, store):
        users.create("boss", "a-good-password", role=users.ADMIN)
        user = users.create("jsmith", "a-good-password")
        token = users.start_session(user["id"])
        assert users.session_user(token)[0] is not None

        users.update(user["id"], active=False)
        assert users.session_user(token)[0] is None


class TestAuthenticating:

    def test_the_right_password_is_accepted(self, store):
        users.create("jsmith", "a-good-password")
        assert users.authenticate("jsmith", "a-good-password")["username"] == "jsmith"

    def test_the_username_is_not_case_sensitive(self, store):
        users.create("jsmith", "a-good-password")
        assert users.authenticate("JSmith", "a-good-password") is not None

    def test_the_wrong_password_is_refused(self, store):
        users.create("jsmith", "a-good-password")
        assert users.authenticate("jsmith", "not-the-password") is None

    def test_an_unknown_user_is_refused(self, store):
        assert users.authenticate("nobody", "a-good-password") is None

    def test_a_disabled_user_cannot_sign_in(self, store):
        users.create("boss", "a-good-password", role=users.ADMIN)
        user = users.create("jsmith", "a-good-password")
        users.update(user["id"], active=False)
        assert users.authenticate("jsmith", "a-good-password") is None

    def test_repeated_failures_lock_the_account(self, store, monkeypatch):
        monkeypatch.setattr(users, "LOGIN_MAX_ATTEMPTS", 3)
        users.create("jsmith", "a-good-password")

        for _ in range(3):
            assert users.authenticate("jsmith", "wrong") is None

        assert users.locked("jsmith") is True
        assert users.authenticate("jsmith", "a-good-password") is None

    def test_a_successful_login_clears_the_failure_count(self, store, monkeypatch):
        monkeypatch.setattr(users, "LOGIN_MAX_ATTEMPTS", 3)
        users.create("jsmith", "a-good-password")

        users.authenticate("jsmith", "wrong")
        users.authenticate("jsmith", "wrong")
        assert users.authenticate("jsmith", "a-good-password") is not None

        users.authenticate("jsmith", "wrong")
        assert users.locked("jsmith") is False

    def test_a_name_with_no_account_is_still_counted(self, store, monkeypatch):
        monkeypatch.setattr(users, "LOGIN_MAX_ATTEMPTS", 3)
        for _ in range(3):
            users.note_failed_login("never-seen-before")

        assert users.locked("never-seen-before") is True
        assert users.by_username("never-seen-before") is None

    def test_counting_a_blank_name_does_nothing(self, store):
        assert users.note_failed_login("") is False
        assert users.note_failed_login(None) is False

    def test_stale_counters_are_forgotten(self, store, monkeypatch):
        from datetime import datetime, timedelta, timezone

        monkeypatch.setattr(users, "LOGIN_LOCKOUT_MINUTES", 15)
        users.note_failed_login("long-ago")
        old = (datetime.now(timezone.utc)
               - timedelta(days=2)).isoformat(timespec="seconds")
        users.connect().execute(
            "UPDATE login_attempts SET last_failure = ?, locked_until = NULL "
            "WHERE username = ?", (old, "long-ago"))

        users.note_failed_login("someone-else")
        remaining = {r["username"] for r in users.connect().execute(
            "SELECT username FROM login_attempts")}
        assert "long-ago" not in remaining
        assert "someone-else" in remaining

    def test_a_lockout_is_not_swept_away_while_it_is_still_running(
            self, store, monkeypatch):
        from datetime import datetime, timedelta, timezone

        monkeypatch.setattr(users, "LOGIN_MAX_ATTEMPTS", 1)
        users.note_failed_login("locked-out")
        old = (datetime.now(timezone.utc)
               - timedelta(days=2)).isoformat(timespec="seconds")
        users.connect().execute(
            "UPDATE login_attempts SET last_failure = ? WHERE username = ?",
            (old, "locked-out"))

        users.note_failed_login("someone-else")
        assert users.locked("locked-out") is True

    def test_an_administrator_can_unlock(self, store, monkeypatch):
        monkeypatch.setattr(users, "LOGIN_MAX_ATTEMPTS", 2)
        user = users.create("jsmith", "a-good-password")
        users.authenticate("jsmith", "wrong")
        users.authenticate("jsmith", "wrong")
        assert users.locked("jsmith") is True

        users.unlock(user["id"])
        assert users.authenticate("jsmith", "a-good-password") is not None

    def test_setting_a_password_unlocks_and_resets(self, store, monkeypatch):
        monkeypatch.setattr(users, "LOGIN_MAX_ATTEMPTS", 2)
        user = users.create("jsmith", "a-good-password")
        users.authenticate("jsmith", "wrong")
        users.authenticate("jsmith", "wrong")

        users.set_password(user["id"], "a-brand-new-password")
        assert users.authenticate("jsmith", "a-brand-new-password") is not None


class TestSessions:

    def test_a_token_resolves_to_its_user(self, store):
        user = users.create("jsmith", "a-good-password")
        token = users.start_session(user["id"], ip="10.0.0.9")
        found, acting = users.session_user(token)
        assert found["username"] == "jsmith"
        assert acting is None

    def test_an_unknown_token_resolves_to_nobody(self, store):
        assert users.session_user("not-a-token") == (None, None)
        assert users.session_user("") == (None, None)
        assert users.session_user(None) == (None, None)

    def test_an_expired_session_is_refused_and_cleaned_up(self, store, monkeypatch):
        monkeypatch.setattr(users, "SESSION_HOURS", -1)
        user = users.create("jsmith", "a-good-password")
        token = users.start_session(user["id"])
        assert users.session_user(token)[0] is None
        assert users.active_sessions() == []

    def test_signing_out_ends_only_that_session(self, store):
        user = users.create("jsmith", "a-good-password")
        here = users.start_session(user["id"])
        elsewhere = users.start_session(user["id"])

        users.end_session(here)
        assert users.session_user(here)[0] is None
        assert users.session_user(elsewhere)[0] is not None

    def test_the_listing_never_carries_a_whole_token(self, store):
        user = users.create("jsmith", "a-good-password")
        token = users.start_session(user["id"])
        listed = users.active_sessions()[0]
        assert "token" not in listed
        assert listed["handle"] == token[:8]
        assert token not in str(listed)

    def test_a_session_can_be_ended_by_its_handle(self, store):
        user = users.create("jsmith", "a-good-password")
        token = users.start_session(user["id"])
        handle = users.active_sessions()[0]["handle"]

        assert users.end_session_by_handle(handle) is True
        assert users.session_user(token)[0] is None

    def test_a_short_handle_is_refused_rather_than_matching_everything(self, store):
        user = users.create("jsmith", "a-good-password")
        users.start_session(user["id"])
        assert users.end_session_by_handle("a") is False
        assert users.end_session_by_handle("") is False
        assert len(users.active_sessions()) == 1

    def test_acting_as_is_reported_alongside_the_real_user(self, store):
        boss = users.create("boss", "a-good-password", role=users.ADMIN)
        user = users.create("jsmith", "a-good-password")
        token = users.start_session(boss["id"])

        users.set_acting_as(token, user["id"])
        real, acting = users.session_user(token)
        assert real["username"] == "boss"
        assert acting["username"] == "jsmith"

    def test_acting_as_a_disabled_user_falls_back_to_the_admin(self, store):
        boss = users.create("boss", "a-good-password", role=users.ADMIN)
        user = users.create("jsmith", "a-good-password")
        token = users.start_session(boss["id"])
        users.set_acting_as(token, user["id"])

        users.update(user["id"], active=False)
        real, acting = users.session_user(token)
        assert real["username"] == "boss"
        assert acting is None


class TestTokens:

    def test_a_token_identifies_its_user(self, store):
        user = users.create("jsmith", "a-good-password")
        token = users.mint_token(user["id"])
        assert users.by_token(token)["username"] == "jsmith"

    def test_minting_replaces_the_previous_token(self, store):
        user = users.create("jsmith", "a-good-password")
        first = users.mint_token(user["id"])
        second = users.mint_token(user["id"])
        assert users.by_token(first) is None
        assert users.by_token(second) is not None

    def test_a_revoked_token_stops_working(self, store):
        user = users.create("jsmith", "a-good-password")
        token = users.mint_token(user["id"])
        users.revoke_token(user["id"])
        assert users.by_token(token) is None

    def test_a_disabled_user_s_token_stops_working(self, store):
        users.create("boss", "a-good-password", role=users.ADMIN)
        user = users.create("jsmith", "a-good-password")
        token = users.mint_token(user["id"])
        users.update(user["id"], active=False)
        assert users.by_token(token) is None

    def test_the_user_record_says_whether_one_exists_not_what_it_is(self, store):
        user = users.create("jsmith", "a-good-password")
        assert users.get(user["id"])["has_token"] is False
        token = users.mint_token(user["id"])
        listed = users.get(user["id"])
        assert listed["has_token"] is True
        assert token not in str(listed)

    def test_no_token_is_not_a_way_in(self, store):
        assert users.by_token(None) is None
        assert users.by_token("") is None


class TestAudit:

    def test_an_entry_records_who_did_what(self, store):
        user = users.create("jsmith", "a-good-password")
        users.audit("data.reloaded", user=user, detail={"app": "sales"}, ip="10.0.0.9")

        entry = users.audit_trail()[0]
        assert entry["username"] == "jsmith"
        assert entry["action"] == "data.reloaded"
        assert "sales" in entry["detail"]
        assert entry["ip"] == "10.0.0.9"

    def test_the_trail_is_newest_first(self, store):
        users.audit("first")
        users.audit("second")
        assert [e["action"] for e in users.audit_trail()][:2] == ["second", "first"]

    def test_it_can_be_filtered(self, store):
        user = users.create("jsmith", "a-good-password")
        other = users.create("bcooper", "a-good-password")
        users.audit("login", user=user)
        users.audit("login", user=other)
        users.audit("data.reloaded", user=user)

        assert len(users.audit_trail(user_id=user["id"])) == 2
        assert len(users.audit_trail(action="login")) == 2
        assert len(users.audit_trail(user_id=user["id"], action="login")) == 1

    def test_a_failed_login_is_recorded_without_an_account(self, store):
        users.audit("login.failed", detail={"username": "nobody"}, ip="10.0.0.9")
        entry = users.audit_trail()[0]
        assert entry["user_id"] is None
        assert "nobody" in entry["detail"]

    def test_writing_an_entry_never_raises(self, store, monkeypatch):
        import sqlite3

        def broken():
            raise sqlite3.OperationalError("disk full")

        monkeypatch.setattr(users, "connect", broken)
        assert users.audit("data.reloaded") is True


class TestBootstrap:

    def test_it_creates_an_administrator_when_there_are_none(self, store):
        password = users.bootstrap()
        admin = users.by_username(users.ADMIN_USERNAME)
        assert admin["is_admin"] is True
        assert users.authenticate(admin["username"], password) is not None

    def test_it_does_nothing_when_an_account_already_exists(self, store):
        users.create("jsmith", "a-good-password")
        assert users.bootstrap() is None
        assert users.count() == 1

    def test_a_configured_password_is_used_and_not_handed_back(self, store,
                                                               monkeypatch):
        monkeypatch.setattr(users, "ADMIN_PASSWORD", "from-the-environment")
        assert users.bootstrap() is None
        assert users.authenticate(users.ADMIN_USERNAME, "from-the-environment")
