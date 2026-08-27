"""Signing in with a Windows username and password.

Users already type their AD credential to reach Qlik Sense. Asking them to
remember a second password for this would be one more thing to forget and
leak, and would mean somebody typing a Qlik identity for every account by
hand before any of it worked.

Everything here runs against ldap3's mock server, so it needs no domain
controller - which is just as well, because the machine this was written on
is in a workgroup.
"""

import pytest

ldap3 = pytest.importorskip("ldap3")

import auth  # noqa: E402
import directory  # noqa: E402
import users  # noqa: E402

pytestmark = pytest.mark.live_auth

BASE = "dc=bank,dc=internal"
JSMITH_DN = f"cn=J Smith,ou=People,{BASE}"
BOSS_DN = f"cn=The Boss,ou=People,{BASE}"
ADMIN_GROUP = f"cn=Qlik AI Admins,ou=Groups,{BASE}"


@pytest.fixture
def ad(monkeypatch):
    """A fake Active Directory with two people in it.

    ldap3's MOCK_SYNC strategy is a real LDAP server implementation over an
    in-memory store: binds succeed or fail on the password actually held, and
    searches run the filter properly - so the filter escaping and the bind
    semantics are genuinely exercised rather than mocked away.

    A connection is built fresh for each bind because the mock strategy
    resets the directory when it attaches to a server, so entries are
    re-added every time. Tests mutate the `people` dict this returns to
    change what AD says about somebody.
    """
    monkeypatch.setattr(directory, "LDAP_ENABLED", True)
    monkeypatch.setattr(directory, "LDAP_SERVER", "dc01.bank.internal")
    monkeypatch.setattr(directory, "LDAP_PORT", 636)
    monkeypatch.setattr(directory, "LDAP_USE_SSL", True)
    monkeypatch.setattr(directory, "LDAP_START_TLS", False)
    monkeypatch.setattr(directory, "LDAP_BASE_DN", BASE)
    monkeypatch.setattr(directory, "LDAP_USER_ATTRIBUTE", "sAMAccountName")
    monkeypatch.setattr(directory, "LDAP_UPN_SUFFIX", "bank.internal")
    monkeypatch.setattr(directory, "LDAP_WINDOWS_DOMAIN", "BANK")
    monkeypatch.setattr(directory, "LDAP_QLIK_DIRECTORY", "BANK")
    monkeypatch.setattr(directory, "LDAP_ADMIN_GROUP", "")
    monkeypatch.setattr(directory, "LDAP_BIND_USER", "")
    monkeypatch.setattr(directory, "LDAP_BIND_PASSWORD", "")
    monkeypatch.setattr(directory, "LDAP_TIMEOUT", 5)

    people = {
        JSMITH_DN: {
            "sAMAccountName": "jsmith", "displayName": "J Smith",
            "mail": "jsmith@bank.internal",
            "userPrincipalName": "jsmith@bank.internal",
            "userPassword": "Windows-Password-1",
            "objectClass": "person",
        },
        BOSS_DN: {
            "sAMAccountName": "boss", "displayName": "The Boss",
            "mail": "boss@bank.internal",
            "userPrincipalName": "boss@bank.internal",
            "userPassword": "Boss-Password-1",
            "memberOf": [ADMIN_GROUP],
            "objectClass": "person",
        },
    }

    # The directory binds on a UPN or a down-level name; the mock binds on a
    # DN, so map between them the way a real DC resolves it internally.
    names = {
        "jsmith@bank.internal": JSMITH_DN, "BANK\\jsmith": JSMITH_DN,
        "boss@bank.internal": BOSS_DN, "BANK\\boss": BOSS_DN,
    }

    def connect(_ldap3, user, password, server=None):
        connection = ldap3.Connection(
            ldap3.Server("fake-dc", get_info=ldap3.OFFLINE_AD_2012_R2),
            user=names.get(user, user), password=password,
            client_strategy=ldap3.MOCK_SYNC, raise_exceptions=False,
        )
        for dn, attributes in people.items():
            connection.strategy.add_entry(dn, dict(attributes))
        return connection, connection.bind()

    monkeypatch.setattr(directory, "_connect", connect)
    return people


# ----------------------------------------------------------------------
# The bind itself
# ----------------------------------------------------------------------

class TestCheckingAPassword:

    def test_the_right_password_returns_the_directory_entry(self, ad):
        entry = directory.authenticate("jsmith", "Windows-Password-1")
        assert entry["username"] == "jsmith"
        assert entry["display_name"] == "J Smith"

    def test_the_wrong_password_is_refused(self, ad):
        assert directory.authenticate("jsmith", "not-it") is None

    def test_an_unknown_user_is_refused(self, ad):
        assert directory.authenticate("nobody", "any-password") is None

    def test_an_empty_password_is_refused_before_it_reaches_the_network(
            self, ad, monkeypatch):
        """A bind with a real name and an empty password is an
        *unauthenticated bind* in the LDAP specification, and directories
        answer success to it. Passing a blank password through would be a way
        to sign in as anybody."""
        def explode(*args, **kwargs):
            pytest.fail("an empty password reached the directory")

        monkeypatch.setattr(directory, "_connect", explode)
        assert directory.authenticate("jsmith", "") is None
        assert directory.authenticate("jsmith", None) is None

    def test_an_empty_username_is_refused(self, ad):
        assert directory.authenticate("", "some-password") is None

    def test_it_is_off_when_not_configured(self, monkeypatch):
        monkeypatch.setattr(directory, "LDAP_ENABLED", False)
        assert directory.enabled() is False
        assert directory.authenticate("jsmith", "Windows-Password-1") is None


class TestHowTheNameIsSent:

    def test_a_bare_name_becomes_a_user_principal_name(self, ad):
        """Preferred because it works across a forest with several domains,
        where the NetBIOS name does not."""
        assert directory.login_name("jsmith") == "jsmith@bank.internal"

    def test_the_down_level_form_is_used_without_a_upn_suffix(self, ad, monkeypatch):
        monkeypatch.setattr(directory, "LDAP_UPN_SUFFIX", "")
        assert directory.login_name("jsmith") == "BANK\\jsmith"

    def test_an_already_qualified_name_is_left_alone(self, ad):
        assert directory.login_name("BANK\\jsmith") == "BANK\\jsmith"
        assert directory.login_name("jsmith@other.domain") == "jsmith@other.domain"

    @pytest.mark.parametrize("typed", [
        "jsmith", "JSmith", "BANK\\jsmith", "jsmith@bank.internal", "  jsmith  ",
    ])
    def test_people_type_it_however_they_type_it_into_qlik(self, ad, typed):
        assert directory.account_name(typed) == "jsmith"

    def test_a_name_with_filter_syntax_in_it_cannot_change_the_search(self, ad):
        """A username containing a parenthesis or asterisk would otherwise
        rewrite the filter rather than being searched for."""
        assert directory.authenticate("js*", "Windows-Password-1") is None
        assert directory.authenticate("jsmith)(cn=*", "Windows-Password-1") is None


class TestWhatIsTakenFromTheEntry:

    def test_the_qlik_identity_comes_from_the_directory(self, ad):
        """The whole reason this is worth doing: sAMAccountName is what
        Qlik's own AD connector uses as the user id, so the two systems name
        the same person the same way without anybody typing it."""
        entry = directory.authenticate("jsmith", "Windows-Password-1")
        assert directory.qlik_identity(entry) == ("BANK", "jsmith")

    def test_the_qlik_directory_can_differ_from_the_domain(self, ad, monkeypatch):
        """It is the name of the QMC's connector, not a fact about AD."""
        monkeypatch.setattr(directory, "LDAP_QLIK_DIRECTORY", "CORPORATE")
        entry = directory.authenticate("jsmith", "Windows-Password-1")
        assert directory.qlik_identity(entry)[0] == "CORPORATE"

    def test_group_membership_decides_administrators(self, ad, monkeypatch):
        monkeypatch.setattr(directory, "LDAP_ADMIN_GROUP", ADMIN_GROUP)
        assert directory.is_admin(directory.authenticate("boss", "Boss-Password-1"))
        assert not directory.is_admin(
            directory.authenticate("jsmith", "Windows-Password-1"))

    def test_nobody_is_promoted_when_no_group_is_configured(self, ad):
        assert directory.is_admin(
            directory.authenticate("boss", "Boss-Password-1")) is False

    def test_group_matching_ignores_case_and_spacing(self, ad, monkeypatch):
        monkeypatch.setattr(directory, "LDAP_ADMIN_GROUP",
                            "  CN=QLIK AI ADMINS,OU=GROUPS,DC=BANK,DC=INTERNAL ")
        assert directory.is_admin(directory.authenticate("boss", "Boss-Password-1"))


class TestWhenTheDirectoryIsTheProblem:

    def test_an_unreachable_server_raises_rather_than_denying(self, ad, monkeypatch):
        """"Your password is wrong" when a domain controller is down sends
        somebody to reset a password that was never the problem."""
        def unreachable(*args, **kwargs):
            raise directory.DirectoryError("connection refused")

        monkeypatch.setattr(directory, "_connect", unreachable)
        with pytest.raises(directory.DirectoryError):
            directory.authenticate("jsmith", "Windows-Password-1")

    def test_a_user_who_binds_but_cannot_be_found_says_which_setting(
            self, ad, monkeypatch):
        monkeypatch.setattr(directory, "LDAP_BASE_DN", "ou=Nowhere," + BASE)
        with pytest.raises(directory.DirectoryError, match="LDAP_BASE_DN"):
            directory.authenticate("jsmith", "Windows-Password-1")

    def test_the_summary_never_carries_a_password(self, ad, monkeypatch):
        monkeypatch.setattr(directory, "LDAP_BIND_PASSWORD", "super-secret")
        assert "super-secret" not in directory.summary()
        assert "LDAPS" in directory.summary()

    def test_the_summary_says_when_tls_is_off(self, ad, monkeypatch):
        monkeypatch.setattr(directory, "LDAP_USE_SSL", False)
        monkeypatch.setattr(directory, "LDAP_START_TLS", False)
        assert "NO TLS" in directory.summary()


# ----------------------------------------------------------------------
# Signing in through the app
# ----------------------------------------------------------------------

class TestConfigurationRefusesASilentlyBrokenSetup:
    """The failure this guards does not look like a failure. Without a Qlik
    user directory every account created from AD gets an empty identity,
    which does not error - it drops all of them onto the server's shared
    connection and the one global lock, so they queue behind each other.
    Nothing on screen says why; it surfaces months later as "the assistant
    is slow", for everybody at once."""

    def reload_config(self, monkeypatch, **settings):
        """Re-import config with only these settings visible.

        The error is matched on its message rather than its class: reloading
        a module rebuilds every class it defines, so the ConfigError raised
        during the reload is a different object from the one imported before
        it, and `pytest.raises(config.ConfigError)` would not catch it.
        """
        import importlib

        import config

        for key in ("LDAP_ENABLED", "LDAP_SERVER", "LDAP_BASE_DN",
                    "LDAP_UPN_SUFFIX", "LDAP_WINDOWS_DOMAIN",
                    "LDAP_QLIK_DIRECTORY"):
            monkeypatch.delenv(key, raising=False)
        for key, value in settings.items():
            monkeypatch.setenv(key, value)
        return importlib.reload(config)

    @pytest.fixture(autouse=True)
    def restore_config(self):
        """Put the real settings back, or every later test sees these.

        The environment is cleared here rather than left to monkeypatch,
        because fixture teardown runs before monkeypatch undoes its own
        changes - so reloading first would re-read the very settings the
        test was proving are invalid, and fail during teardown.
        """
        yield
        import importlib
        import os

        for key in ("LDAP_ENABLED", "LDAP_SERVER", "LDAP_BASE_DN",
                    "LDAP_UPN_SUFFIX", "LDAP_WINDOWS_DOMAIN",
                    "LDAP_QLIK_DIRECTORY"):
            os.environ.pop(key, None)

        import config

        importlib.reload(config)

    def test_a_upn_suffix_alone_is_refused(self, monkeypatch):
        with pytest.raises(Exception, match="Qlik user directory"):
            self.reload_config(
                monkeypatch, LDAP_ENABLED="true", LDAP_SERVER="dc01",
                LDAP_BASE_DN="DC=x", LDAP_UPN_SUFFIX="x.internal")

    def test_a_windows_domain_is_enough(self, monkeypatch):
        reloaded = self.reload_config(
            monkeypatch, LDAP_ENABLED="true", LDAP_SERVER="dc01",
            LDAP_BASE_DN="DC=x", LDAP_WINDOWS_DOMAIN="BANK")
        assert reloaded.LDAP_QLIK_DIRECTORY == "BANK"

    def test_naming_it_explicitly_wins(self, monkeypatch):
        """It is the name of the QMC's connector, which need not be the
        domain."""
        reloaded = self.reload_config(
            monkeypatch, LDAP_ENABLED="true", LDAP_SERVER="dc01",
            LDAP_BASE_DN="DC=x", LDAP_WINDOWS_DOMAIN="BANK",
            LDAP_QLIK_DIRECTORY="CORPORATE")
        assert reloaded.LDAP_QLIK_DIRECTORY == "CORPORATE"

    def test_neither_is_refused(self, monkeypatch):
        with pytest.raises(Exception, match="neither"):
            self.reload_config(monkeypatch, LDAP_ENABLED="true",
                               LDAP_SERVER="dc01", LDAP_BASE_DN="DC=x")


class TestAccountsCreateThemselves:
    """The point of the arrangement: nobody types a Qlik user directory for
    three hundred people, and the value is right because it came from the
    same place Qlik reads."""

    def test_a_first_sign_in_makes_the_account(self, ad):
        assert users.by_username("jsmith") is None

        user, reason = auth.sign_in("jsmith", "Windows-Password-1")

        assert reason is None
        assert user["username"] == "jsmith"
        assert user["display_name"] == "J Smith"
        assert user["qlik_directory"] == "BANK"
        assert user["qlik_user_id"] == "jsmith"
        assert user["from_directory"] is True

    def test_it_has_no_usable_local_password(self, ad):
        """A local password on a directory account would be a second way in
        that AD does not know about, and would not close when they leave."""
        auth.sign_in("jsmith", "Windows-Password-1")
        assert users.authenticate("jsmith", "Windows-Password-1") is None
        assert users.authenticate("jsmith", "") is None

    def test_a_second_sign_in_reuses_the_account(self, ad):
        first, _ = auth.sign_in("jsmith", "Windows-Password-1")
        second, _ = auth.sign_in("jsmith", "Windows-Password-1")
        assert first["id"] == second["id"]
        assert users.count() == 1

    def test_directory_attributes_are_refreshed_every_time(self, ad):
        """AD stays the authority rather than this database drifting."""
        auth.sign_in("jsmith", "Windows-Password-1")
        ad[JSMITH_DN]["displayName"] = "Jane Smith-Jones"

        user, _ = auth.sign_in("jsmith", "Windows-Password-1")
        assert user["display_name"] == "Jane Smith-Jones"

    def test_the_admin_group_promotes_and_demotes(self, ad, monkeypatch):
        monkeypatch.setattr(directory, "LDAP_ADMIN_GROUP", ADMIN_GROUP)
        # Somebody has to remain an administrator for a demotion to be legal.
        users.create("keeper", "keeper-password-1", role=users.ADMIN)

        boss, _ = auth.sign_in("boss", "Boss-Password-1")
        assert boss["is_admin"] is True

        ad[BOSS_DN]["memberOf"] = []
        boss, _ = auth.sign_in("boss", "Boss-Password-1")
        assert boss["is_admin"] is False

    def test_being_disabled_here_survives_a_successful_bind(self, ad):
        """Disabling somebody is a local decision about this tool, and a
        working Windows password must not quietly undo it."""
        users.create("keeper", "keeper-password-1", role=users.ADMIN)
        auth.sign_in("jsmith", "Windows-Password-1")
        users.update(users.by_username("jsmith")["id"], active=False)

        user, reason = auth.sign_in("jsmith", "Windows-Password-1")
        assert user is None
        assert reason == "credentials"


class TestLocalAndDirectoryAccountsCoexist:

    def test_a_local_administrator_still_works(self, ad):
        """Break-glass: the account that gets in when AD is unreachable."""
        users.create("breakglass", "local-password-1", role=users.ADMIN)
        user, reason = auth.sign_in("breakglass", "local-password-1")
        assert reason is None
        assert user["from_directory"] is False

    def test_a_local_account_is_not_checked_against_the_directory(
            self, ad, monkeypatch):
        users.create("breakglass", "local-password-1", role=users.ADMIN)

        def explode(*args, **kwargs):
            pytest.fail("a local account was sent to the directory")

        monkeypatch.setattr(directory, "authenticate", explode)
        assert auth.sign_in("breakglass", "local-password-1")[1] is None

    def test_a_directory_account_is_not_checked_locally(self, ad, monkeypatch):
        auth.sign_in("jsmith", "Windows-Password-1")

        def explode(*args, **kwargs):
            pytest.fail("a directory account was checked against a local password")

        monkeypatch.setattr(users, "authenticate", explode)
        auth.sign_in("jsmith", "Windows-Password-1")

    def test_a_directory_account_is_refused_when_the_directory_is_off(
            self, ad, monkeypatch):
        """There is no local password to fall back to, and inventing one
        would be a way in that AD never authorised."""
        auth.sign_in("jsmith", "Windows-Password-1")
        monkeypatch.setattr(directory, "LDAP_ENABLED", False)

        user, reason = auth.sign_in("jsmith", "Windows-Password-1")
        assert user is None
        assert reason == "directory"

    def test_an_unreachable_directory_does_not_lock_out_local_admins(
            self, ad, monkeypatch):
        users.create("breakglass", "local-password-1", role=users.ADMIN)

        def unreachable(*args, **kwargs):
            raise directory.DirectoryError("the DC is down")

        monkeypatch.setattr(directory, "authenticate", unreachable)
        assert auth.sign_in("breakglass", "local-password-1")[1] is None
        assert auth.sign_in("jsmith", "Windows-Password-1")[1] == "directory"


class TestTheLockoutProtectsTheWindowsAccount:
    """The most important thing in this file. Our rate limit runs BEFORE the
    bind, so somebody hammering this login form locks the account here rather
    than locking the person's Windows account across the whole bank."""

    def test_failures_against_the_directory_are_counted(self, ad, monkeypatch):
        monkeypatch.setattr(users, "LOGIN_MAX_ATTEMPTS", 3)
        auth.sign_in("jsmith", "Windows-Password-1")     # creates the account

        for _ in range(3):
            assert auth.sign_in("jsmith", "wrong")[1] == "credentials"

        assert users.locked("jsmith") is True

    def test_a_locked_account_never_reaches_the_directory(self, ad, monkeypatch):
        monkeypatch.setattr(users, "LOGIN_MAX_ATTEMPTS", 2)
        auth.sign_in("jsmith", "Windows-Password-1")
        auth.sign_in("jsmith", "wrong")
        auth.sign_in("jsmith", "wrong")

        def explode(*args, **kwargs):
            pytest.fail("a locked account was sent on to Active Directory")

        monkeypatch.setattr(directory, "authenticate", explode)
        assert auth.sign_in("jsmith", "Windows-Password-1")[1] == "locked"

    def test_someone_who_has_never_signed_in_is_protected_too(self, ad,
                                                              monkeypatch):
        """The case the ordering exists for, and the one it originally
        missed. With directory sign-in an account creates itself on FIRST
        sign-in, so everybody who has not used the product yet has no local
        row - and a lockout counted against that row gave those people no
        protection at all. Every guess reached the domain controller, which
        is how an attacker walks a real employee's Windows account into AD's
        own lockout through this login form."""
        monkeypatch.setattr(users, "LOGIN_MAX_ATTEMPTS", 3)

        reached = []
        real = directory.authenticate

        def counting(username, password):
            reached.append(username)
            return real(username, password)

        monkeypatch.setattr(directory, "authenticate", counting)

        assert users.by_username("jsmith") is None
        for _ in range(12):
            auth.sign_in("jsmith", "guessing")

        assert len(reached) == 3, (
            f"{len(reached)} guesses reached the directory; the lockout "
            f"should have stopped it at 3")
        assert users.locked("jsmith") is True

    def test_guessing_does_not_create_accounts(self, ad):
        """Otherwise the login form is a way to fill the accounts table."""
        for i in range(5):
            auth.sign_in(f"nobody{i}", "guessing")
        assert users.count() == 0

    def test_the_lockout_survives_a_correct_password(self, ad, monkeypatch):
        monkeypatch.setattr(users, "LOGIN_MAX_ATTEMPTS", 2)
        auth.sign_in("jsmith", "wrong")
        auth.sign_in("jsmith", "wrong")

        user, reason = auth.sign_in("jsmith", "Windows-Password-1")
        assert user is None and reason == "locked"

    def test_a_successful_sign_in_clears_the_count(self, ad, monkeypatch):
        monkeypatch.setattr(users, "LOGIN_MAX_ATTEMPTS", 3)
        auth.sign_in("jsmith", "wrong")
        auth.sign_in("jsmith", "wrong")
        assert auth.sign_in("jsmith", "Windows-Password-1")[1] is None

        auth.sign_in("jsmith", "wrong")
        assert users.locked("jsmith") is False

    def test_an_administrator_can_unlock_a_name_with_no_account(self, ad,
                                                                monkeypatch):
        monkeypatch.setattr(users, "LOGIN_MAX_ATTEMPTS", 2)
        auth.sign_in("newcomer", "wrong")
        auth.sign_in("newcomer", "wrong")
        assert users.locked("newcomer") is True

        users.unlock_name("newcomer")
        assert users.locked("newcomer") is False

    def test_an_administrator_can_unlock_from_the_command_line(self, ad, monkeypatch):
        import manage_users

        monkeypatch.setattr(users, "LOGIN_MAX_ATTEMPTS", 2)
        auth.sign_in("jsmith", "Windows-Password-1")
        auth.sign_in("jsmith", "wrong")
        auth.sign_in("jsmith", "wrong")

        assert manage_users.main(["unlock", "jsmith"]) == 0
        assert auth.sign_in("jsmith", "Windows-Password-1")[1] is None
