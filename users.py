"""Accounts, logins and the audit trail.

Everything above this module talks about *a user*; this is where a user is a
row rather than an idea. It holds three tables and nothing else - no HTTP, no
Qlik, no cookies (those are auth.py's job), so it can be exercised from a
test or a script without a server running.

SQLite rather than a JSON file, which is what conversations use. The reason
they differ is that they are read differently: a conversation is only ever
read or written whole by one writer, and a directory of readable JSON is
something you can back up or delete by hand. Accounts are the opposite -
looked up on every single request, written by several requests at once, and
queried in ways a flat file cannot answer ("who logged in yesterday", "which
sessions belong to the user I just disabled"). sqlite3 is in the standard
library, so this costs no new dependency.

Passwords are stored as PBKDF2-HMAC-SHA256 with a per-user salt, in a
self-describing format that records the cost - so the cost can be raised
later without invalidating what is already stored.
"""

import hashlib
import hmac
import json
import logging
import re
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import (
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    LOGIN_LOCKOUT_MINUTES,
    LOGIN_MAX_ATTEMPTS,
    PASSWORD_MIN,
    PBKDF2_ROUNDS,
    SESSION_HOURS,
    USERS_DB,
)

log = logging.getLogger(__name__)

ADMIN = "admin"
USER = "user"
ROLES = (ADMIN, USER)

# Where an account's password is checked. An account is one or the other and
# never both: a local password on a directory account would be a second way
# in that AD does not know about, and so would not close when the person
# leaves.
LOCAL = "local"
DIRECTORY = "ldap"
SOURCES = (LOCAL, DIRECTORY)

# Usernames are typed by hand and shown everywhere, so the alphabet is
# deliberately narrow. Anything outside it is rejected at the door rather
# than escaped later.
USERNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}\Z")

# One connection per thread. SQLite objects belong to the thread that made
# them, and uvicorn serves requests on a pool of them.
_local = threading.local()
_init_lock = threading.Lock()
_initialised = set()


class UserError(Exception):
    """Something the caller can fix: a bad username, a short password."""


# ----------------------------------------------------------------------
# Storage
# ----------------------------------------------------------------------

_resolved = {}


def _path():
    """Where the database lives, with its directory created on first use.

    "On first use" used to mean on every use: this ran expanduser() and
    mkdir() on each call, and connect() calls it for every database access
    the product makes. Measured at 50 microseconds a call, against 0.04 for
    everything else connect() does.

    It is also a syscall against the filesystem on the path of every login,
    every audit line and every session lookup - which matters if the folder
    is ever put on a network share, where that syscall is a round trip that
    can fail or hang. Once per path is enough; the tests point USERS_DB
    somewhere temporary, so the answer is cached against the setting rather
    than in a plain global.
    """
    cached = _resolved.get(USERS_DB)
    if cached is not None:
        return cached
    path = Path(USERS_DB).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    _resolved[USERS_DB] = path
    return path


def _usable(db):
    """Whether a cached handle is still open.

    A connection is kept per thread, so a handle that dies stays cached and
    every later request that worker picks up fails the same way - signing in
    included. Which requests broke then depended on which thread happened to
    serve them, so the server looked half-alive and stayed that way until
    somebody restarted it.

    `in_transaction` is a property on the connection object that raises once
    the handle is closed, so it answers the question without a query: it was
    measured at 0.04 microseconds against 16 for `PRAGMA user_version`, and
    this runs on every database call the product makes.

    It detects a closed handle, which is the failure that persists. It does
    not detect a database that is present but unhappy - a full disk, a file
    locked by a backup agent - and nor would reconnecting fix those. Those
    surface as an error on the request that hit them, and the next request
    tries again on the same handle, which is the correct behaviour: the
    handle is not what is broken.
    """
    try:
        db.in_transaction
        return True
    except sqlite3.Error as e:
        log.warning("Discarding a dead account-database handle: %s", e)
        return False


def connect():
    """This thread's connection, opened and migrated on first use."""
    path = str(_path())
    existing = getattr(_local, "db", None)
    if existing is not None and getattr(_local, "path", None) == path:
        if _usable(existing):
            return existing
        # Dead. Fall through and open a new one rather than handing back a
        # handle that will raise - one broken worker thread should heal on
        # its next request, not stay broken for the life of the process.
        _local.db = None
        _local.path = None
        try:
            existing.close()
        except sqlite3.Error:
            pass
        existing = None

    if existing is not None:
        # The path changed under us - a test pointing USERS_DB somewhere
        # temporary. Drop the old handle rather than answering from it.
        existing.close()

    db = sqlite3.connect(path, timeout=10, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    _local.db = db
    _local.path = path

    # Once per file, not once per thread: several threads opening their
    # first connection together would otherwise all run the schema.
    with _init_lock:
        if path not in _initialised:
            _migrate(db)
            _initialised.add(path)
    return db


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    username       TEXT    NOT NULL UNIQUE,
    display_name   TEXT    NOT NULL DEFAULT '',
    password       TEXT    NOT NULL,
    role           TEXT    NOT NULL DEFAULT 'user',
    qlik_directory TEXT    NOT NULL DEFAULT '',
    qlik_user_id   TEXT    NOT NULL DEFAULT '',
    active         INTEGER NOT NULL DEFAULT 1,
    api_token      TEXT    UNIQUE,
    auth_source    TEXT    NOT NULL DEFAULT 'local',
    created        TEXT    NOT NULL,
    updated        TEXT    NOT NULL,
    last_login     TEXT,
    failed_logins  INTEGER NOT NULL DEFAULT 0,
    locked_until   TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT    PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    acting_as  INTEGER          REFERENCES users(id) ON DELETE SET NULL,
    created    TEXT    NOT NULL,
    expires    TEXT    NOT NULL,
    last_seen  TEXT    NOT NULL,
    ip         TEXT    NOT NULL DEFAULT '',
    agent      TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS login_attempts (
    username     TEXT    PRIMARY KEY,
    failures     INTEGER NOT NULL DEFAULT 0,
    last_failure TEXT    NOT NULL,
    locked_until TEXT
);
CREATE INDEX IF NOT EXISTS login_attempts_seen ON login_attempts(last_failure);

CREATE TABLE IF NOT EXISTS audit (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    at       TEXT    NOT NULL,
    user_id  INTEGER,
    username TEXT    NOT NULL DEFAULT '',
    action   TEXT    NOT NULL,
    detail   TEXT    NOT NULL DEFAULT '',
    ip       TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS audit_at ON audit(at);
CREATE INDEX IF NOT EXISTS audit_user ON audit(user_id);
"""


# Columns added after the first release. CREATE TABLE IF NOT EXISTS does
# nothing to a table that already exists, so a database made before a column
# was introduced needs it added explicitly - otherwise the upgrade fails on
# the first query rather than at startup.
ADDED_COLUMNS = (
    ("users", "auth_source", "TEXT NOT NULL DEFAULT 'local'"),
)


def _migrate(db):
    db.executescript(SCHEMA)
    for table, column, definition in ADDED_COLUMNS:
        existing = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            log.info("Adding %s.%s to an existing database", table, column)
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _later(hours):
    return (datetime.now(timezone.utc)
            + timedelta(hours=hours)).isoformat(timespec="seconds")


def _past(iso):
    """True when an ISO timestamp is in the past, or unreadable."""
    if not iso:
        return True
    try:
        when = datetime.fromisoformat(iso)
    except ValueError:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when <= datetime.now(timezone.utc)


def reset_for_tests():
    """Forget the cached connection so USERS_DB can be repointed."""
    db = getattr(_local, "db", None)
    if db is not None:
        db.close()
    _local.db = None
    _local.path = None
    # The resolved path is cached against the setting, and a test's temporary
    # directory is deleted between runs - so a cached path would stop being
    # created again for a USERS_DB value that came round twice.
    _resolved.clear()
    with _init_lock:
        _initialised.clear()


# ----------------------------------------------------------------------
# Passwords
# ----------------------------------------------------------------------

def hash_password(password, rounds=None, salt=None):
    """PBKDF2-HMAC-SHA256, in a format that records its own cost.

    'pbkdf2_sha256$rounds$salt$hash'. Raising PBKDF2_ROUNDS later leaves
    every stored password still verifiable - check_password reads the cost
    from the value rather than from configuration.
    """
    rounds = rounds or PBKDF2_ROUNDS
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return f"pbkdf2_sha256${rounds}${salt.hex()}${digest.hex()}"


def check_password(password, stored):
    """Constant-time verification. False for anything malformed."""
    try:
        algorithm, rounds, salt, digest = str(stored).split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        computed = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt), int(rounds)
        )
    except (AttributeError, ValueError):
        return False
    return hmac.compare_digest(computed.hex(), digest)


def unusable_password():
    """A hash nothing can match, for accounts whose password lives elsewhere.

    Random rather than a marker string, so a bug that somehow reached
    check_password with it still fails rather than matching a known value.
    """
    return "pbkdf2_sha256$1$" + secrets.token_hex(16) + "$" + secrets.token_hex(32)


def check_password_quality(password):
    if len(password or "") < PASSWORD_MIN:
        raise UserError(f"A password needs at least {PASSWORD_MIN} characters.")
    return password


# ----------------------------------------------------------------------
# Users
# ----------------------------------------------------------------------

def clean_username(username):
    name = str(username or "").strip().lower()
    if not USERNAME_PATTERN.match(name):
        raise UserError(
            "A username is 2-32 characters, starts with a letter or digit, "
            "and holds only letters, digits, dot, dash and underscore."
        )
    return name


# Every read of a user joins this, so `failed_logins` and `locked_until`
# keep the shape they have always had while the authority for them moves.
USER_COLUMNS = """
    SELECT u.*,
           COALESCE(a.failures, 0)  AS attempt_failures,
           a.locked_until           AS attempt_locked_until
    FROM users u
    LEFT JOIN login_attempts a ON a.username = u.username
"""


def _row(row):
    """A row as the rest of the app sees it - never carrying the password."""
    if row is None:
        return None
    user = dict(row)
    user.pop("password", None)
    # The lockout is keyed on the typed username, not on this row - see
    # note_failed_login. Presented under the old names so nothing above has
    # to know that.
    if "attempt_failures" in user:
        user["failed_logins"] = user.pop("attempt_failures")
        user["locked_until"] = user.pop("attempt_locked_until")
    user["active"] = bool(user.get("active", 1))
    user["is_admin"] = user.get("role") == ADMIN
    user["auth_source"] = user.get("auth_source") or LOCAL
    user["from_directory"] = user["auth_source"] == DIRECTORY
    # The token is a credential; only the endpoint that just minted one
    # reveals it, so it is not handed out with the user by default.
    user["has_token"] = bool(user.pop("api_token", None))
    return user


def create(username, password, role=USER, display_name="",
           qlik_directory="", qlik_user_id="", active=True, auth_source=LOCAL):
    """Add a user. The username is the identity; everything else can change.

    A directory account is given an unusable password rather than none:
    every row has a hash, so nothing downstream has to handle the absence of
    one, and no password can ever match it.
    """
    name = clean_username(username)
    if auth_source not in SOURCES:
        raise UserError("Auth source must be one of " + ", ".join(SOURCES) + ".")

    if auth_source == DIRECTORY:
        stored = unusable_password()
    else:
        check_password_quality(password)
        stored = hash_password(password)

    if role not in ROLES:
        raise UserError("Role must be one of " + ", ".join(ROLES) + ".")

    now = _now()
    try:
        cursor = connect().execute(
            "INSERT INTO users (username, display_name, password, role, "
            "qlik_directory, qlik_user_id, active, auth_source, created, updated) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name, str(display_name or "").strip(), stored, role,
             str(qlik_directory or "").strip(), str(qlik_user_id or "").strip(),
             1 if active else 0, auth_source, now, now),
        )
    except sqlite3.IntegrityError as e:
        raise UserError(f"There is already a user called {name!r}.") from e
    return get(cursor.lastrowid)


def get(user_id):
    return _row(connect().execute(
        USER_COLUMNS + " WHERE u.id = ?", (user_id,)).fetchone())


def by_username(username):
    name = str(username or "").strip().lower()
    return _row(connect().execute(
        USER_COLUMNS + " WHERE u.username = ?", (name,)).fetchone())


def by_token(token):
    """The user holding an API token, for MCP clients that cannot use cookies."""
    if not token:
        return None
    return _row(connect().execute(
        USER_COLUMNS + " WHERE u.api_token = ? AND u.active = 1",
        (str(token),)).fetchone())


def listing():
    """Every account, admins first, then alphabetically."""
    rows = connect().execute(
        USER_COLUMNS + " ORDER BY u.role = 'admin' DESC, u.username"
    ).fetchall()
    return [_row(r) for r in rows]


def count(role=None):
    if role:
        return connect().execute(
            "SELECT COUNT(*) FROM users WHERE role = ? AND active = 1", (role,)
        ).fetchone()[0]
    return connect().execute("SELECT COUNT(*) FROM users").fetchone()[0]


# Fields the admin page may change. Listed rather than derived, so a new
# column is not silently writable from the browser.
EDITABLE = ("display_name", "role", "qlik_directory", "qlik_user_id", "active")


def update(user_id, **fields):
    """Change a user. Refuses to remove the last way in.

    Demoting or disabling the only active admin locks everyone out of user
    management permanently - the account that could undo it is the one being
    changed. It is refused here rather than in the endpoint, so a script
    cannot do what the page will not.
    """
    user = get(user_id)
    if user is None:
        raise UserError("No such user.")

    unknown = set(fields) - set(EDITABLE)
    if unknown:
        raise UserError("Cannot change " + ", ".join(sorted(unknown)) + ".")

    if "role" in fields and fields["role"] not in ROLES:
        raise UserError("Role must be one of " + ", ".join(ROLES) + ".")

    losing_admin = (
        user["is_admin"] and user["active"]
        and (fields.get("role", ADMIN) != ADMIN or not fields.get("active", True))
    )
    if losing_admin and count(ADMIN) <= 1:
        raise UserError(
            "This is the only active administrator. Promote someone else "
            "first, or there would be nobody left who can manage users."
        )

    sets, values = [], []
    for key, value in fields.items():
        sets.append(f"{key} = ?")
        if key == "active":
            values.append(1 if value else 0)
        else:
            values.append(str(value or "").strip())
    sets.append("updated = ?")
    values += [_now(), user_id]

    connect().execute(
        "UPDATE users SET " + ", ".join(sets) + " WHERE id = ?", values)

    if "active" in fields and not fields["active"]:
        # A disabled account must stop being able to act immediately, not at
        # the end of a 12-hour cookie.
        end_sessions(user_id)
    return get(user_id)


def set_password(user_id, password):
    check_password_quality(password)
    if get(user_id) is None:
        raise UserError("No such user.")
    connect().execute(
        "UPDATE users SET password = ?, updated = ? WHERE id = ?",
        (hash_password(password), _now(), user_id),
    )
    clear_failed_logins(get(user_id)["username"])
    return True


def delete(user_id):
    """Remove an account. The last active admin is not removable."""
    user = get(user_id)
    if user is None:
        return False
    if user["is_admin"] and user["active"] and count(ADMIN) <= 1:
        raise UserError(
            "This is the only active administrator, so deleting it would "
            "leave nobody who can manage users."
        )
    end_sessions(user_id)
    connect().execute("DELETE FROM users WHERE id = ?", (user_id,))
    return True


def mint_token(user_id):
    """Give a user an API token for the MCP endpoint, replacing any before it.

    Returned once, in the clear, because there is nowhere to look it up
    afterwards - only whether one exists.
    """
    if get(user_id) is None:
        raise UserError("No such user.")
    token = secrets.token_urlsafe(32)
    connect().execute(
        "UPDATE users SET api_token = ?, updated = ? WHERE id = ?",
        (token, _now(), user_id),
    )
    return token


def revoke_token(user_id):
    connect().execute(
        "UPDATE users SET api_token = NULL, updated = ? WHERE id = ?",
        (_now(), user_id),
    )
    return True


# ----------------------------------------------------------------------
# Logging in
# ----------------------------------------------------------------------

def authenticate(username, password):
    """The user, or None. Wrong password and no such user look identical.

    Repeated failures lock the account for a while. Without that, a password
    of eight characters is a few hours of guessing over a LAN - and this is
    pointed at a bank's figures.

    The lockout is checked here as well as in auth.sign_in, which checks it
    before anything reaches a domain controller. Two checks rather than one
    because this function is reachable on its own - from the command line, or
    from any caller that has not been through the HTTP path - and a lockout
    that only exists in the layer above is a lockout that a second entry
    point walks straight past.
    """
    name = str(username or "").strip().lower()
    row = connect().execute(
        "SELECT * FROM users WHERE username = ?", (name,)).fetchone()

    if row is None:
        # Spend roughly what a real check would, so the response time does
        # not say whether the account exists.
        hash_password(password or "")
        return None

    if not row["active"]:
        return None

    if locked(name):
        return None

    if not check_password(password or "", row["password"]):
        note_failed_login(name)
        return None

    clear_failed_logins(name)
    connect().execute(
        "UPDATE users SET last_login = ? WHERE id = ?", (_now(), row["id"]))
    return get(row["id"])


def clean_username_soft(username):
    r"""A username normalised for lookup, without raising on a bad one.

    The login form must not answer differently for "no such user" and
    "that is not a valid username" - both are just a failed sign-in, and
    telling them apart is a way to enumerate the alphabet.

    A domain-qualified name is accepted and reduced, because people type
    what they type into Qlik: BANK\jsmith and jsmith@bank.internal are
    both jsmith here.
    """
    name = str(username or "").strip()
    if "\\" in name:
        name = name.split("\\", 1)[1]
    if "@" in name:
        name = name.split("@", 1)[0]
    name = name.lower()
    return name if USERNAME_PATTERN.match(name) else ""


def note_failed_login(username):
    """Count a failed sign-in against the NAME that was typed.

    Keyed on the typed username rather than on a local account row, and that
    distinction is the whole point. With directory sign-in an account creates
    itself on FIRST sign-in, so everybody who has not used the product yet has
    no row here - and counting against the row meant those people had no
    lockout at all. Every guess against them reached the domain controller,
    which is how an attacker drives a real employee's Windows account into
    AD's own lockout through this login form. Exactly the failure the
    ordering was supposed to prevent, for exactly the users it was supposed
    to protect.
    """
    name = str(username or "").strip().lower()
    if not name:
        return False

    now = _now()
    db = connect()
    db.execute(
        "INSERT INTO login_attempts (username, failures, last_failure) "
        "VALUES (?, 1, ?) "
        "ON CONFLICT(username) DO UPDATE SET "
        "  failures = failures + 1, last_failure = excluded.last_failure",
        (name, now),
    )

    failures = db.execute(
        "SELECT failures FROM login_attempts WHERE username = ?",
        (name,)).fetchone()[0]

    if failures >= LOGIN_MAX_ATTEMPTS:
        until = (datetime.now(timezone.utc) + timedelta(
            minutes=LOGIN_LOCKOUT_MINUTES)).isoformat(timespec="seconds")
        db.execute(
            "UPDATE login_attempts SET locked_until = ? WHERE username = ?",
            (until, name))
        log.warning("Locking %r after %d failed sign-ins", name, failures)

    _forget_stale_attempts()
    return True


def _forget_stale_attempts():
    """Drop counters nobody has touched since well past the lockout window.

    This table is written by an endpoint that needs no credentials, so a
    row per invented username is a way to fill a disk. Self-cleaning keeps
    it to roughly "names tried recently" rather than "names ever tried".
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(
        minutes=max(LOGIN_LOCKOUT_MINUTES * 4, 60))).isoformat(timespec="seconds")
    connect().execute(
        "DELETE FROM login_attempts WHERE last_failure < ? AND "
        "(locked_until IS NULL OR locked_until < ?)", (cutoff, _now()))


def clear_failed_logins(username):
    """Forget the failures for a name - a successful sign-in, or an unlock."""
    connect().execute("DELETE FROM login_attempts WHERE username = ?",
                      (str(username or "").strip().lower(),))
    return True


def from_directory(entry, qlik_identity, role=None):
    """Find or create the local record for somebody Active Directory knows.

    The account creates itself on first sign-in, which is the point of the
    whole arrangement: nobody types a Qlik user directory for three hundred
    people, and the value is right because it came from the same place Qlik
    reads.

    Everything AD owns is refreshed on every sign-in - a renamed person, a
    changed identity, a promotion into or out of the administrators group -
    so the directory stays the authority rather than this database drifting
    away from it.

    `role` is the directory's answer, or None when no group is configured to
    decide it - in which case the role stays whatever an administrator set
    here. It is passed in rather than worked out from configuration, so the
    policy lives in one place instead of being read again from underneath.

    What is NOT taken from AD is `active`: an administrator disabling
    somebody here is a local decision about this tool, and a successful bind
    should not quietly undo it.
    """
    if role is not None and role not in ROLES:
        raise UserError("Role must be one of " + ", ".join(ROLES) + ".")
    name = clean_username_soft(entry.get("username"))
    if not name:
        raise UserError("The directory returned a username this system cannot use.")

    qlik_directory, qlik_user_id = qlik_identity

    existing = by_username(name)
    if existing is None:
        user = create(
            name, password=None, role=role or USER,
            display_name=entry.get("display_name") or "",
            qlik_directory=qlik_directory, qlik_user_id=qlik_user_id,
            auth_source=DIRECTORY,
        )
        audit("user.created", user=user,
              detail={"username": name, "from": "directory"})
    else:
        fields = {
            "display_name": entry.get("display_name") or existing["display_name"],
            "qlik_directory": qlik_directory,
            "qlik_user_id": qlik_user_id,
        }
        # Only when a group is configured to decide it. With none, the role
        # stays whatever an administrator set here.
        if role is not None:
            fields["role"] = role
        # update() refuses to demote the last administrator, and that guard
        # is right even when the demotion comes from AD - there would be
        # nobody left to fix it.
        try:
            user = update(existing["id"], **fields)
        except UserError as e:
            log.warning("Could not apply directory attributes to %s: %s", name, e)
            user = existing

    clear_failed_logins(name)
    connect().execute(
        "UPDATE users SET last_login = ? WHERE id = ?", (_now(), user["id"]))
    return get(user["id"])


def locked(username):
    """Whether this NAME is currently locked out, account or no account.

    Answered for names with no local row too, which is what stops an attacker
    walking a real employee's Windows account into AD's lockout before that
    employee has ever signed in here.
    """
    row = connect().execute(
        "SELECT locked_until FROM login_attempts WHERE username = ?",
        (str(username or "").strip().lower(),)).fetchone()
    return bool(row and row["locked_until"] and not _past(row["locked_until"]))


def unlock(user_id):
    """Clear a lockout for an account. See unlock_name for one without."""
    user = get(user_id)
    if user is None:
        return False
    return clear_failed_logins(user["username"])


def unlock_name(username):
    """Clear a lockout for a typed name, whether or not it has an account."""
    return clear_failed_logins(username)


# ----------------------------------------------------------------------
# Sessions
# ----------------------------------------------------------------------

def start_session(user_id, ip="", agent=""):
    """Mint a session token. Kept server-side so it can be revoked."""
    token = secrets.token_urlsafe(32)
    now = _now()
    connect().execute(
        "INSERT INTO sessions (token, user_id, created, expires, last_seen, ip, agent) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (token, user_id, now, _later(SESSION_HOURS), now,
         str(ip or "")[:80], str(agent or "")[:200]),
    )
    return token


def session_user(token):
    """(user, acting_as) for a token, or (None, None).

    `acting_as` is the account an administrator is standing in for. The
    caller decides which of the two owns the work; both are returned so the
    audit trail can name the real person as well as the borrowed identity.
    """
    if not token:
        return None, None
    row = connect().execute(
        "SELECT * FROM sessions WHERE token = ?", (str(token),)).fetchone()
    if row is None:
        return None, None

    if _past(row["expires"]):
        end_session(token)
        return None, None

    user = get(row["user_id"])
    if user is None or not user["active"]:
        end_session(token)
        return None, None

    connect().execute(
        "UPDATE sessions SET last_seen = ? WHERE token = ?", (_now(), token))

    acting_as = get(row["acting_as"]) if row["acting_as"] else None
    if acting_as is not None and not acting_as["active"]:
        acting_as = None
    return user, acting_as


def set_acting_as(token, user_id):
    """Point a session at another user, or back at its owner with None."""
    connect().execute(
        "UPDATE sessions SET acting_as = ? WHERE token = ?", (user_id, str(token)))
    return True


def end_session(token):
    connect().execute("DELETE FROM sessions WHERE token = ?", (str(token),))
    return True


def end_sessions(user_id):
    """Log a user out everywhere, and drop anyone standing in for them."""
    connect().execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    connect().execute(
        "UPDATE sessions SET acting_as = NULL WHERE acting_as = ?", (user_id,))
    return True


def active_sessions():
    """Who is logged in now, for the admin page."""
    rows = connect().execute(
        "SELECT s.token, s.user_id, s.acting_as, s.created, s.expires, "
        "       s.last_seen, s.ip, u.username, u.display_name "
        "FROM sessions s JOIN users u ON u.id = s.user_id "
        "ORDER BY s.last_seen DESC"
    ).fetchall()
    out = []
    for row in rows:
        record = dict(row)
        # The token itself is a credential. The admin page needs a handle to
        # revoke a session with, not the key to walk into it.
        record["handle"] = record.pop("token")[:8]
        record["expired"] = _past(record["expires"])
        out.append(record)
    return out


def end_session_by_handle(handle):
    """Revoke a session the admin page named by its short handle."""
    handle = str(handle or "")
    if len(handle) < 6:
        return False
    row = connect().execute(
        "SELECT token FROM sessions WHERE token LIKE ?", (handle + "%",)).fetchone()
    if row is None:
        return False
    return end_session(row["token"])


def purge_expired():
    connect().execute("DELETE FROM sessions WHERE expires <= ?", (_now(),))
    return True


# ----------------------------------------------------------------------
# Audit
# ----------------------------------------------------------------------

def audit(action, user=None, detail=None, ip=""):
    """Record something that happened. Never fatal to the thing it records.

    An audit write failing should cost the trail, not the user's request -
    but it is logged loudly, because a silent gap in an audit trail is worse
    than a noisy one.
    """
    try:
        connect().execute(
            "INSERT INTO audit (at, user_id, username, action, detail, ip) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (_now(),
             (user or {}).get("id"),
             (user or {}).get("username", ""),
             str(action),
             detail if isinstance(detail, str)
             else json.dumps(detail or {}, default=str),
             str(ip or "")[:80]),
        )
    except sqlite3.Error:
        log.exception("Could not write the audit record for %r", action)
    return True


def audit_trail(limit=200, user_id=None, action=None, since=None):
    where, values = [], []
    if user_id:
        where.append("user_id = ?")
        values.append(user_id)
    if action:
        where.append("action = ?")
        values.append(action)
    if since:
        where.append("at >= ?")
        values.append(since)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    values.append(max(1, min(int(limit), 2000)))
    rows = connect().execute(
        f"SELECT * FROM audit {clause} ORDER BY id DESC LIMIT ?", values).fetchall()
    return [dict(r) for r in rows]


def prune_audit(days=None, dry_run=False):
    """Drop audit records older than the retention policy.

    Returns how many were removed, or would be. Zero days keeps everything,
    which is the default: silently deleting a bank's audit trail is a worse
    failure than keeping too much of it.
    """
    from config import AUDIT_RETENTION_DAYS

    days = AUDIT_RETENTION_DAYS if days is None else days
    if days <= 0:
        return 0

    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=days)).isoformat(timespec="seconds")
    doomed = connect().execute(
        "SELECT COUNT(*) FROM audit WHERE at < ?", (cutoff,)).fetchone()[0]
    if doomed and not dry_run:
        connect().execute("DELETE FROM audit WHERE at < ?", (cutoff,))
        # Recorded in the trail it just trimmed, so the gap is explained
        # rather than looking like tampering.
        audit("audit.pruned", detail={"removed": doomed, "older_than": cutoff})
        log.info("Removed %d audit record(s) older than %s", doomed, cutoff)
    return doomed


def audit_actions():
    """Every action name recorded so far, for the admin page's filter."""
    return [r[0] for r in connect().execute(
        "SELECT DISTINCT action FROM audit ORDER BY action").fetchall()]


# ----------------------------------------------------------------------
# First run
# ----------------------------------------------------------------------

def bootstrap():
    """Make sure there is a way in. Returns a password only if it made one.

    A system with no accounts and a login page is a locked door with the key
    inside. The first administrator is created here, with the password from
    ADMIN_PASSWORD if one is set and a generated one otherwise - returned to
    the caller to print once, because it is not stored anywhere readable.
    """
    connect()
    if count() > 0:
        return None

    password = ADMIN_PASSWORD or secrets.token_urlsafe(12)
    create(ADMIN_USERNAME, password, role=ADMIN, display_name="Administrator")
    audit("bootstrap", detail={"username": ADMIN_USERNAME})
    log.info("Created the first administrator %r", ADMIN_USERNAME)
    return None if ADMIN_PASSWORD else password
