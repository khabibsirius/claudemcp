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

LOCAL = "local"
DIRECTORY = "ldap"
SOURCES = (LOCAL, DIRECTORY)

USERNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}\Z")

_local = threading.local()
_init_lock = threading.Lock()
_initialised = set()


class UserError(Exception):
    pass


_resolved = {}


def _path():
    cached = _resolved.get(USERS_DB)
    if cached is not None:
        return cached
    path = Path(USERS_DB).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    _resolved[USERS_DB] = path
    return path


def _usable(db):
    try:
        db.in_transaction
        return True
    except sqlite3.Error as e:
        log.warning("Discarding a dead account-database handle: %s", e)
        return False


def connect():
    path = str(_path())
    existing = getattr(_local, "db", None)
    if existing is not None and getattr(_local, "path", None) == path:
        if _usable(existing):
            return existing
        _local.db = None
        _local.path = None
        try:
            existing.close()
        except sqlite3.Error:
            pass
        existing = None

    if existing is not None:
        existing.close()

    db = sqlite3.connect(path, timeout=10, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    _local.db = db
    _local.path = path

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
    db = getattr(_local, "db", None)
    if db is not None:
        db.close()
    _local.db = None
    _local.path = None
    _resolved.clear()
    with _init_lock:
        _initialised.clear()


def hash_password(password, rounds=None, salt=None):
    rounds = rounds or PBKDF2_ROUNDS
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return f"pbkdf2_sha256${rounds}${salt.hex()}${digest.hex()}"


def check_password(password, stored):
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
    return "pbkdf2_sha256$1$" + secrets.token_hex(16) + "$" + secrets.token_hex(32)


def check_password_quality(password):
    if len(password or "") < PASSWORD_MIN:
        raise UserError(f"A password needs at least {PASSWORD_MIN} characters.")
    return password


def clean_username(username):
    name = str(username or "").strip().lower()
    if not USERNAME_PATTERN.match(name):
        raise UserError(
            "A username is 2-32 characters, starts with a letter or digit, "
            "and holds only letters, digits, dot, dash and underscore."
        )
    return name


USER_COLUMNS = """
    SELECT u.*,
           COALESCE(a.failures, 0)  AS attempt_failures,
           a.locked_until           AS attempt_locked_until
    FROM users u
    LEFT JOIN login_attempts a ON a.username = u.username
"""


def _row(row):
    if row is None:
        return None
    user = dict(row)
    user.pop("password", None)
    if "attempt_failures" in user:
        user["failed_logins"] = user.pop("attempt_failures")
        user["locked_until"] = user.pop("attempt_locked_until")
    user["active"] = bool(user.get("active", 1))
    user["is_admin"] = user.get("role") == ADMIN
    user["auth_source"] = user.get("auth_source") or LOCAL
    user["from_directory"] = user["auth_source"] == DIRECTORY
    user["has_token"] = bool(user.pop("api_token", None))
    return user


def create(username, password, role=USER, display_name="",
           qlik_directory="", qlik_user_id="", active=True, auth_source=LOCAL):
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
    if not token:
        return None
    return _row(connect().execute(
        USER_COLUMNS + " WHERE u.api_token = ? AND u.active = 1",
        (str(token),)).fetchone())


def listing():
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


EDITABLE = ("display_name", "role", "qlik_directory", "qlik_user_id", "active")


def update(user_id, **fields):
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


def authenticate(username, password):
    name = str(username or "").strip().lower()
    row = connect().execute(
        "SELECT * FROM users WHERE username = ?", (name,)).fetchone()

    if row is None:
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
    name = str(username or "").strip()
    if "\\" in name:
        name = name.split("\\", 1)[1]
    if "@" in name:
        name = name.split("@", 1)[0]
    name = name.lower()
    return name if USERNAME_PATTERN.match(name) else ""


def note_failed_login(username):
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
    cutoff = (datetime.now(timezone.utc) - timedelta(
        minutes=max(LOGIN_LOCKOUT_MINUTES * 4, 60))).isoformat(timespec="seconds")
    connect().execute(
        "DELETE FROM login_attempts WHERE last_failure < ? AND "
        "(locked_until IS NULL OR locked_until < ?)", (cutoff, _now()))


def clear_failed_logins(username):
    connect().execute("DELETE FROM login_attempts WHERE username = ?",
                      (str(username or "").strip().lower(),))
    return True


def from_directory(entry, qlik_identity, role=None):
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
        if role is not None:
            fields["role"] = role
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
    row = connect().execute(
        "SELECT locked_until FROM login_attempts WHERE username = ?",
        (str(username or "").strip().lower(),)).fetchone()
    return bool(row and row["locked_until"] and not _past(row["locked_until"]))


def unlock(user_id):
    user = get(user_id)
    if user is None:
        return False
    return clear_failed_logins(user["username"])


def unlock_name(username):
    return clear_failed_logins(username)


def start_session(user_id, ip="", agent=""):
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
    connect().execute(
        "UPDATE sessions SET acting_as = ? WHERE token = ?", (user_id, str(token)))
    return True


def end_session(token):
    connect().execute("DELETE FROM sessions WHERE token = ?", (str(token),))
    return True


def end_sessions(user_id):
    connect().execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    connect().execute(
        "UPDATE sessions SET acting_as = NULL WHERE acting_as = ?", (user_id,))
    return True


def active_sessions():
    rows = connect().execute(
        "SELECT s.token, s.user_id, s.acting_as, s.created, s.expires, "
        "       s.last_seen, s.ip, u.username, u.display_name "
        "FROM sessions s JOIN users u ON u.id = s.user_id "
        "ORDER BY s.last_seen DESC"
    ).fetchall()
    out = []
    for row in rows:
        record = dict(row)
        record["handle"] = record.pop("token")[:8]
        record["expired"] = _past(record["expires"])
        out.append(record)
    return out


def end_session_by_handle(handle):
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


def audit(action, user=None, detail=None, ip=""):
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
        audit("audit.pruned", detail={"removed": doomed, "older_than": cutoff})
        log.info("Removed %d audit record(s) older than %s", doomed, cutoff)
    return doomed


def audit_actions():
    return [r[0] for r in connect().execute(
        "SELECT DISTINCT action FROM audit ORDER BY action").fetchall()]


def bootstrap():
    connect()
    if count() > 0:
        return None

    password = ADMIN_PASSWORD or secrets.token_urlsafe(12)
    create(ADMIN_USERNAME, password, role=ADMIN, display_name="Administrator")
    audit("bootstrap", detail={"username": ADMIN_USERNAME})
    log.info("Created the first administrator %r", ADMIN_USERNAME)
    return None if ADMIN_PASSWORD else password
