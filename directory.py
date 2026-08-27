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

ATTRIBUTES = ["sAMAccountName", "displayName", "mail", "userPrincipalName",
              "memberOf", "distinguishedName"]


class DirectoryError(Exception):
    pass

def enabled():
    return bool(LDAP_ENABLED and LDAP_SERVER)


def _ldap3():
    try:
        import ldap3
    except ImportError as e:
        raise DirectoryError(
            "LDAP_ENABLED is on but the ldap3 package is not installed. "
            "Run: pip install -r requirements.txt"
        ) from e
    return ldap3


def login_name(username):
    name = str(username or "").strip()
    if "@" in name or "\\" in name:
        return name
    if LDAP_UPN_SUFFIX:
        return f"{name}@{LDAP_UPN_SUFFIX.lstrip('@')}"
    if LDAP_WINDOWS_DOMAIN:
        return f"{LDAP_WINDOWS_DOMAIN}\\{name}"
    return name


def account_name(username):
    name = str(username or "").strip()
    if "\\" in name:
        name = name.split("\\", 1)[1]
    if "@" in name:
        name = name.split("@", 1)[0]
    return name.lower()


def _server(ldap3):
    if not LDAP_USE_SSL and not LDAP_START_TLS:
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
    try:
        connection = ldap3.Connection(
            server or _server(ldap3),
            user=user, password=password,
            auto_bind=False, raise_exceptions=False,
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
    if not enabled():
        return None

    if not password:
        return None
    if not str(username or "").strip():
        return None

    ldap3 = _ldap3()
    connection, bound = _connect(ldap3, login_name(username), password)

    if not bound:
        log.info("Directory refused %s: %s", account_name(username),
                 connection.result.get("description"))
        _close(connection)
        return None

    try:
        entry = _find(ldap3, connection, account_name(username))
    finally:
        _close(connection)

    if entry is None:
        raise DirectoryError(
            f"{account_name(username)} signed in but could not be found under "
            f"{LDAP_BASE_DN!r}. Check LDAP_BASE_DN covers this user."
        )
    return entry


def _find(ldap3, connection, sam_account_name):
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


def qlik_identity(entry):
    directory = LDAP_QLIK_DIRECTORY or LDAP_WINDOWS_DOMAIN or ""
    return directory, (entry.get("username") or "")


def decides_role():
    return bool(LDAP_ADMIN_GROUP)


def is_admin(entry):
    if not LDAP_ADMIN_GROUP:
        return False
    wanted = LDAP_ADMIN_GROUP.strip().lower()
    return any(group.strip().lower() == wanted for group in entry.get("groups", []))


def summary():
    if not enabled():
        return "directory sign-in off - accounts are local to this server"
    security = ("LDAPS" if LDAP_USE_SSL else
                "StartTLS" if LDAP_START_TLS else "NO TLS - passwords in clear")
    return (f"{LDAP_SERVER}:{LDAP_PORT} ({security}), base {LDAP_BASE_DN!r}, "
            f"matching {LDAP_USER_ATTRIBUTE}")
