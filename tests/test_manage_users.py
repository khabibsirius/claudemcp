"""The command line recovery tool.

It exists for one failure with no other way out: five wrong passwords lock an
account for fifteen minutes, and on an installation with one administrator
that locks the only person who could unlock it - behind the login that is
refusing them. So the tests that matter most here are the recovery ones.
"""

import pytest

import manage_users
import users

pytestmark = pytest.mark.live_auth


def run(*argv):
    return manage_users.main(list(argv))


@pytest.fixture
def boss():
    return users.create("boss", "boss-password-1", role=users.ADMIN,
                        display_name="The Boss")


@pytest.fixture
def member():
    return users.create("jsmith", "jsmith-password-1", display_name="J Smith")


# ----------------------------------------------------------------------
# The reason it exists
# ----------------------------------------------------------------------

class TestRecoveringALockedOutAdministrator:

    def test_it_unlocks_the_only_administrator(self, boss, monkeypatch, capsys):
        monkeypatch.setattr(users, "LOGIN_MAX_ATTEMPTS", 3)
        for _ in range(3):
            users.authenticate("boss", "wrong")
        assert users.locked("boss") is True
        assert users.authenticate("boss", "boss-password-1") is None

        assert run("unlock", "boss") == 0

        assert users.locked("boss") is False
        assert users.authenticate("boss", "boss-password-1") is not None
        assert "unlocked" in capsys.readouterr().out

    def test_it_resets_a_forgotten_password(self, boss, capsys):
        assert run("password", "boss", "--password", "a-brand-new-password") == 0
        assert users.authenticate("boss", "a-brand-new-password") is not None
        assert users.authenticate("boss", "boss-password-1") is None

    def test_a_reset_signs_them_out_everywhere(self, boss):
        """The sessions open now were opened with the old password."""
        token = users.start_session(boss["id"])
        run("password", "boss", "--password", "a-brand-new-password")
        assert users.session_user(token)[0] is None

    def test_it_can_make_a_second_administrator(self, boss):
        """The other way out of the same corner."""
        assert run("create", "deputy", "--admin",
                   "--password", "deputy-password-1") == 0
        assert users.by_username("deputy")["is_admin"] is True

    def test_unlocking_someone_not_locked_is_not_an_error(self, boss, capsys):
        assert run("unlock", "boss") == 0
        assert "not locked" in capsys.readouterr().out

    def test_it_works_with_no_server_running(self, boss):
        """It talks to the database, not to the service - which is the whole
        point, because the service is usually the thing that is wrong."""
        import web_app  # noqa: F401  - imported, never started

        assert run("list") == 0


# ----------------------------------------------------------------------
# Everyday use
# ----------------------------------------------------------------------

class TestListing:

    def test_it_shows_everyone(self, boss, member, capsys):
        assert run("list") == 0
        out = capsys.readouterr().out
        assert "boss" in out and "jsmith" in out

    def test_it_marks_a_locked_account(self, boss, member, monkeypatch, capsys):
        monkeypatch.setattr(users, "LOGIN_MAX_ATTEMPTS", 1)
        users.authenticate("jsmith", "wrong")
        run("list")
        assert "LOCKED" in capsys.readouterr().out

    def test_it_marks_a_disabled_account(self, boss, member, capsys):
        users.update(member["id"], active=False)
        run("list")
        assert "disabled" in capsys.readouterr().out

    def test_it_shows_the_qlik_identity(self, boss, member, capsys):
        users.update(member["id"], qlik_directory="BANK", qlik_user_id="jsmith")
        run("list")
        assert "BANK\\jsmith" in capsys.readouterr().out

    def test_the_column_fits_the_header(self, capsys):
        """Short usernames must not narrow the column below its own title."""
        users.create("ab", "a-good-password", role=users.ADMIN)
        run("list")
        lines = capsys.readouterr().out.splitlines()
        assert lines[0].startswith("USERNAME  ")
        assert lines[1].startswith("ab        ")

    def test_an_empty_database_says_so_rather_than_printing_a_header(self, capsys):
        assert run("list") == 0
        assert "No accounts yet" in capsys.readouterr().out


class TestCreating:

    def test_it_sets_the_qlik_identity(self, boss):
        run("create", "jsmith", "--password", "jsmith-password-1",
            "--qlik-directory", "BANK", "--qlik-user", "jsmith")
        made = users.by_username("jsmith")
        assert (made["qlik_directory"], made["qlik_user_id"]) == ("BANK", "jsmith")

    def test_it_warns_when_there_is_no_qlik_identity(self, boss, capsys):
        """On Enterprise these accounts share one connection and one lock, so
        a rollout that forgets this has a queue nobody can see."""
        run("create", "jsmith", "--password", "jsmith-password-1")
        assert "share the server's connection" in capsys.readouterr().out

    def test_a_bad_username_is_refused_with_a_reason(self, boss, capsys):
        assert run("create", "not a username", "--password", "a-good-password") == 1
        assert "username" in capsys.readouterr().err.lower()

    def test_a_short_password_is_refused(self, boss, capsys):
        assert run("create", "jsmith", "--password", "no") == 1
        assert "at least" in capsys.readouterr().err


class TestChanging:

    def test_disabling_and_enabling(self, boss, member):
        assert run("disable", "jsmith") == 0
        assert users.authenticate("jsmith", "jsmith-password-1") is None
        assert run("enable", "jsmith") == 0
        assert users.authenticate("jsmith", "jsmith-password-1") is not None

    def test_disabling_ends_their_sessions(self, boss, member):
        token = users.start_session(member["id"])
        run("disable", "jsmith")
        assert users.session_user(token)[0] is None

    def test_promoting_and_demoting(self, boss, member):
        assert run("role", "jsmith", "admin") == 0
        assert users.by_username("jsmith")["is_admin"] is True
        assert run("role", "jsmith", "user") == 0
        assert users.by_username("jsmith")["is_admin"] is False

    def test_the_last_administrator_is_protected(self, boss, capsys):
        """The same guard the web page has - a script must not be able to do
        what the page will not."""
        assert run("role", "boss", "user") == 1
        assert "only active administrator" in capsys.readouterr().err

    def test_setting_the_qlik_identity(self, boss, member, capsys):
        assert run("qlik", "jsmith", "BANK", "jane.smith") == 0
        assert users.by_username("jsmith")["qlik_user_id"] == "jane.smith"


class TestTokens:

    def test_it_issues_one(self, boss, capsys):
        assert run("token", "boss") == 0
        printed = capsys.readouterr().out
        token = printed.split()[-1]
        assert users.by_token(token)["username"] == "boss"

    def test_it_warns_for_a_non_administrator(self, boss, member, capsys):
        run("token", "jsmith")
        assert "administrators only" in capsys.readouterr().err

    def test_it_revokes(self, boss, capsys):
        run("token", "boss")
        token = capsys.readouterr().out.split()[-1]
        assert run("token", "boss", "--revoke") == 0
        assert users.by_token(token) is None


class TestSessionsAndAudit:

    def test_it_lists_who_is_signed_in(self, boss, member, capsys):
        users.start_session(member["id"], ip="10.0.0.9")
        run("sessions")
        out = capsys.readouterr().out
        assert "jsmith" in out and "10.0.0.9" in out

    def test_it_ends_a_session(self, boss, member, capsys):
        token = users.start_session(member["id"])
        run("sessions")
        handle = [line.split()[0] for line in capsys.readouterr().out.splitlines()
                  if "jsmith" in line][0]

        assert run("sessions", "--end", handle) == 0
        assert users.session_user(token)[0] is None

    def test_nobody_signed_in_says_so(self, boss, capsys):
        assert run("sessions") == 0
        assert "Nobody is signed in" in capsys.readouterr().out

    def test_the_audit_trail_can_be_read_and_filtered(self, boss, member, capsys):
        users.audit("login.failed", detail={"username": "jsmith"})
        users.audit("data.reloaded", user=member)

        run("audit", "--action", "login.failed")
        out = capsys.readouterr().out
        assert "login.failed" in out and "data.reloaded" not in out


class TestEverythingIsRecorded:
    """A command line that quietly bypassed the audit trail would be a way to
    change accounts without leaving a mark."""

    @pytest.mark.parametrize("argv,action", [
        (("create", "newbie", "--password", "newbie-password-1"), "user.created"),
        (("password", "jsmith", "--password", "another-password"), "password.reset"),
        (("unlock", "jsmith"), "user.unlocked"),
        (("disable", "jsmith"), "user.updated"),
        (("qlik", "jsmith", "BANK", "js"), "user.updated"),
        (("token", "jsmith"), "token.minted"),
    ])
    def test_the_action_reaches_the_trail(self, boss, member, argv, action):
        assert run(*argv) == 0
        recorded = [e for e in users.audit_trail(limit=200) if e["action"] == action]
        assert recorded, f"{argv[0]} left no audit record"
        assert "command line" in recorded[0]["detail"]


class TestPasswordPrompting:

    def test_it_asks_rather_than_taking_it_from_the_command_line(
            self, boss, monkeypatch):
        """A password in an argument ends up in the shell history."""
        asked = []
        monkeypatch.setattr(manage_users.getpass, "getpass",
                            lambda prompt: asked.append(prompt) or "typed-password-1")
        assert run("password", "boss") == 0
        assert len(asked) == 2          # typed twice
        assert users.authenticate("boss", "typed-password-1") is not None

    def test_a_mistyped_repeat_is_refused(self, boss, monkeypatch, capsys):
        answers = iter(["first-password", "second-password"])
        monkeypatch.setattr(manage_users.getpass, "getpass",
                            lambda prompt: next(answers))
        assert run("password", "boss") == 1
        assert "did not match" in capsys.readouterr().err
        assert users.authenticate("boss", "boss-password-1") is not None
