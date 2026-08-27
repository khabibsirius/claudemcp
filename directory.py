"""Checking a password against Active Directory.

Your users already type their Windows username and password to reach Qlik
Sense. Asking them to remember a *second* password for this is the wrong
answer twice over: it is one more credential to forget, reset and leak, and
it means somebody has to type a Qlik identity for every account by hand
before any of it works.

So this verifies the credential they already have, against the same
directory Qlik reads. On success it returns what AD knows about them, and
two of those attributes are exactly what the Qlik session needs:

    sAMAccountName  ->  qlik_user_id
    the domain      ->  qlik_directory

which is why an account can create itself correctly on first sign-in rather
than being prepared in advance for hundreds of people.

**This is a bind, not a directory read.** The password is proven by asking a
domain controller to accept it, and it is never stored here - not hashed,
not cached. The only thing kept locally is that the person exists, what role
they have, and their conversations.

Three things about doing this safely, each of which is a real hole rather
than a precaution:

- **TLS is not optional.** A simple bind sends the password in the clear.
  LDAPS on 636, or StartTLS on 389. Turning it off is possible and says so
  loudly, because somebody will need it to diagnose a certificate problem.
- **An empty password must be refused before the bind.** A bind with a valid
  DN and an empty password is an *unauthenticated bind* in the LDAP
  specification, and directories answer "success" to it. Passing a blank
  password straight through is a way to log in as anybody.
- **Failures are counted before the bind, not after.** Our own lockout runs
  first, so somebody hammering this login form locks the account *here*
  rather than locking their Windows account across the whole bank.
"""

import logging

from config import (
    LDAP_ADMIN_GROUP,
    LDAP_BASE_DN,
    LDAP_BIND_PASSWORD,
    LDAP_BIND_USER,
    LDAP_ENABLED,
    LDAP_PORT,
    LDAP_QLIK_DIRECTORY,
    LDAP_SERVER,
    LDAP_START_TLS,
    LDAP_TIMEOUT,
    LDAP_UPN_SUFFIX,
    LDAP_USE_SSL,
    LDAP_USER_ATTRIBUTE,
    LDAP_WINDOWS_DOMAIN,
)

log = logging.getLogger(__name__)

# Read on every sign-in. displayName and mail are cosmetic; sAMAccountName is
# load-bearing, because it is what Qlik's own AD connector uses as the user
# id, and memberOf decides who administers this.
ATTRIBUTES = ["sAMAccountName", "displayName", "mail", "userPrincipalName",
              "memberOf", "distinguishedName"]


class DirectoryError(Exception):
    """The directory could not be reached or answered unusably.

    Deliberately distinct from "wrong password": one is the person's fault
    and the other is the infrastructure's, and telling a user their password
    is wrong when a domain controller is down sends them to reset a password
    that was never the problem.
    """


def enabled():
    return bool(LDAP_ENABLED and LDAP_SERVER)


def _ldap3():
    """Imported here so the package is only needed where it is used.

    A desktop install with local accounts should not have to install an LDAP
    library, and an import at module scope would make it a hard dependency of
    even running the tests.
    """
    try:
        import ldap3
    except ImportError as e:  # pragma: no cover - the package is in requirements
        raise DirectoryError(
            "LDAP_ENABLED is on but the ldap3 package is not installed. "
            "Run: pip install -r requirements.txt"
        ) from e
    return ldap3


def login_name(username):
    """The name to bind with.

    Active Directory accepts a user principal name (jsmith@bank.internal) or
    the down-level form (BANK\\jsmith). The UPN is preferred because it works
    across a forest with several domains, where the NetBIOS name does not.
    """
    name = str(username or "").strip()
    if "@" in name or "\\" in name:
        return name          # already qualified; take it as given
    if LDAP_UPN_SUFFIX:
        return f"{name}@{LDAP_UPN_SUFFIX.lstrip('@')}"
    if LDAP_WINDOWS_DOMAIN:
        return f"{LDAP_WINDOWS_DOMAIN}\\{name}"
    return name


def account_name(username):
    """The bare sAMAccountName, however the person typed it."""
    name = str(username or "").strip()
    if "\\" in name:
        name = name.split("\\", 1)[1]
    if "@" in name:
        name = name.split("@", 1)[0]
    return name.lower()


def _server(ldap3):
    if not LDAP_USE_SSL and not LDAP_START_TLS:
        # Said out loud every time, because a simple bind over plain LDAP
        # puts the user's Windows password on the wire in cleartext - and
        # this is the setting somebody turns off to get past a certificate
        # problem and then forgets.
        log.warning(
            "LDAP is configured without TLS: passwords will cross the network "
            "in the clear. Set LDAP_USE_SSL=true (port 636) or "
            "LDAP_START_TLS=true (port 389)."
        )
    return ldap3.Server(
        LDAP_SERVER, port=LDAP_PORT, use_ssl=LDAP_USE_SSL,
        get_info=ldap3.NONE, connect_timeout=LDAP_TIMEOUT,
    )


def _connect(ldap3, user, password, server=None):
    """Bind as `user`, or raise DirectoryError if the server is unreachable."""
    try:
        connection = ldap3.Connection(
            server or _server(ldap3),
            user=user, password=password,
            auto_bind=False, raise_exceptions=False,
            # AD scatters referrals through its answers and chasing them
            # opens connections to other domain controllers, which is slow
            # and sometimes hangs behind a firewall.
            auto_referrals=False,
            receive_timeout=LDAP_TIMEOUT,
        )
        if LDAP_START_TLS and not LDAP_USE_SSL:
            connection.start_tls()
        bound = connection.bind()
    except Exception as e:
        raise DirectoryError(
            f"Could not reach the directory at {LDAP_SERVER}:{LDAP_PORT}: {e}"
        ) from e
    return connection, bound


def authenticate(username, password):
    """The person's directory entry, or None if the password is wrong.

    Raises DirectoryError when the directory itself is the problem, so the
    caller can tell "your password is wrong" apart from "the domain
    controller is down" - and can fall back to a local account rather than
    locking everybody out of a working server.
    """
    if not enabled():
        return None

    # Before anything reaches the network. A bind with a real name and an
    # empty password is an unauthenticated bind, and directories answer
    # "success" to it - so this check is the difference between a login form
    # and a door.
    if not password:
        return None
    if not str(username or "").strip():
        return None

    ldap3 = _ldap3()
    connection, bound = _connect(ldap3, login_name(username), password)

    if not bound:
        # A bind failure is the password being wrong. The server's own
        # message can say more - "account disabled", "password expired" -
        # but it is not handed to the browser, because it also distinguishes
        # a real username from an invented one.
        log.info("Directory refused %s: %s", account_name(username),
                 connection.result.get("description"))
        _close(connection)
        return None

    try:
        entry = _find(ldap3, connection, account_name(username))
    finally:
        _close(connection)

    if entry is None:
        # Bound successfully but not findable: usually a base DN that does
        # not cover this user's part of the tree.
        raise DirectoryError(
            f"{account_name(username)} signed in but could not be found under "
            f"{LDAP_BASE_DN!r}. Check LDAP_BASE_DN covers this user."
        )
    return entry


def _find(ldap3, connection, sam_account_name):
    """Read the attributes we care about, as the user who just bound.

    A service account is used instead when one is configured, for directories
    that do not let ordinary users read their own entry.
    """
    searcher, opened = connection, False
    if LDAP_BIND_USER:
        searcher, bound = _connect(ldap3, LDAP_BIND_USER, LDAP_BIND_PASSWORD)
        opened = True
        if not bound:
            _close(searcher)
            raise DirectoryError(
                "LDAP_BIND_USER could not sign in to the directory. Check "
                "LDAP_BIND_USER and LDAP_BIND_PASSWORD."
            )

    try:
        # The name is escaped rather than interpolated: a username with a
        # parenthesis or a backslash in it would otherwise change the shape
        # of the filter rather than being searched for.
        escaped = ldap3.utils.conv.escape_filter_chars(sam_account_name)
        searcher.search(
            search_base=LDAP_BASE_DN,
            search_filter=f"({LDAP_USER_ATTRIBUTE}={escaped})",
            attributes=ATTRIBUTES,
        )
        entries = list(searcher.entries or [])
        if not entries:
            return None
        return _as_dict(entries[0])
    except DirectoryError:
        raise
    except Exception as e:
        raise DirectoryError(f"Could not read the directory entry: {e}") from e
    finally:
        if opened:
            _close(searcher)


def _as_dict(entry):
    """One ldap3 entry as plain Python, whatever its attribute types."""
    def value(name):
        try:
            attribute = entry[name]
        except (KeyError, LookupError):
            return ""
        raw = attribute.value
        if isinstance(raw, list):
            return raw[0] if raw else ""
        return raw if raw is not None else ""

    def values(name):
        try:
            attribute = entry[name]
        except (KeyError, LookupError):
            return []
        raw = attribute.value
        if raw is None:
            return []
        return list(raw) if isinstance(raw, list) else [raw]

    return {
        "username": str(value("sAMAccountName")).lower(),
        "display_name": str(value("displayName")),
        "email": str(value("mail")),
        "upn": str(value("userPrincipalName")),
        "dn": str(value("distinguishedName")),
        "groups": [str(g) for g in values("memberOf")],
    }


def _close(connection):
    try:
        connection.unbind()
    except Exception:
        log.debug("Could not unbind cleanly", exc_info=True)


# ----------------------------------------------------------------------
# What the rest of the app takes from an entry
# ----------------------------------------------------------------------

def qlik_identity(entry):
    """(user directory, user id) for the Qlik session.

    Qlik's own Active Directory connector imports users with the
    sAMAccountName as the user id, and names the user directory after the
    connector - which is conventionally the NetBIOS domain but does not have
    to be. LDAP_QLIK_DIRECTORY exists for when it is not: get it wrong and
    the engine opens a session as a user the QMC has never heard of, which
    fails as a permissions error rather than as a configuration one.
    """
    directory = LDAP_QLIK_DIRECTORY or LDAP_WINDOWS_DOMAIN or ""
    return directory, (entry.get("username") or "")


def decides_role():
    """Whether Active Directory is the authority on who administers this.

    With no group configured it is not, and roles stay whatever an
    administrator set here - so turning group control on is a deliberate act
    rather than something that silently starts demoting people.
    """
    return bool(LDAP_ADMIN_GROUP)


def is_admin(entry):
    """Whether AD says this person administers the assistant.

    Group membership rather than a role kept here, so joiners and leavers are
    handled where the bank already handles them. With no group configured
    nobody is promoted this way and roles stay local.
    """
    if not LDAP_ADMIN_GROUP:
        return False
    wanted = LDAP_ADMIN_GROUP.strip().lower()
    return any(group.strip().lower() == wanted for group in entry.get("groups", []))


def summary():
    """Human-readable settings, for check_connection.py. Never the password."""
    if not enabled():
        return "directory sign-in off - accounts are local to this server"
    security = ("LDAPS" if LDAP_USE_SSL else
                "StartTLS" if LDAP_START_TLS else "NO TLS - passwords in clear")
    return (f"{LDAP_SERVER}:{LDAP_PORT} ({security}), base {LDAP_BASE_DN!r}, "
            f"matching {LDAP_USER_ATTRIBUTE}")
