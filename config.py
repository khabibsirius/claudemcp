import os

from dotenv import load_dotenv

load_dotenv()


class ConfigError(Exception):
    pass

def _str_env(name, default=""):
    return (os.getenv(name) or default).strip()


def _int_env(name, default):
    raw = _str_env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}")


def _float_env(name, default):
    raw = _str_env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number of seconds, got {raw!r}")


def _bool_env(name, default):
    raw = _str_env(name).lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be true or false, got {raw!r}")


DESKTOP = "desktop"
ENTERPRISE = "enterprise"

QLIK_MODE = _str_env("QLIK_MODE", DESKTOP).lower()
if QLIK_MODE not in (DESKTOP, ENTERPRISE):
    raise ConfigError(
        f"QLIK_MODE must be '{DESKTOP}' or '{ENTERPRISE}', got {QLIK_MODE!r}"
    )

QLIK_HOST = _str_env("QLIK_HOST", "localhost")
QLIK_PORT = _int_env("QLIK_PORT", 4747 if QLIK_MODE == ENTERPRISE else 4848)
APP_NAME = _str_env("APP_NAME", "data")

QLIK_CONNECT_TIMEOUT = _float_env("QLIK_CONNECT_TIMEOUT", 10.0)
QLIK_REQUEST_TIMEOUT = _float_env("QLIK_REQUEST_TIMEOUT", 60.0)

RECONNECT_COOLDOWN_SECONDS = _float_env("RECONNECT_COOLDOWN_SECONDS", 15.0)

REQUIRE_QLIK_AT_STARTUP = _bool_env("REQUIRE_QLIK_AT_STARTUP", False)

QLIK_CERT_DIR = _str_env("QLIK_CERT_DIR")
QLIK_USER_DIRECTORY = _str_env("QLIK_USER_DIRECTORY")
QLIK_USER_ID = _str_env("QLIK_USER_ID")
QLIK_SSL_VERIFY = _bool_env("QLIK_SSL_VERIFY", True)

CLIENT_CERT = "client.pem"
CLIENT_KEY = "client_key.pem"
ROOT_CERT = "root.pem"



MODEL_NUM_CTX = _int_env("MODEL_NUM_CTX", 32768)

CHAT_HISTORY_RESERVE = 19_500

CHAT_HISTORY_CHARS = _int_env(
    "CHAT_HISTORY_CHARS", max(24_000, (MODEL_NUM_CTX - CHAT_HISTORY_RESERVE) * 3)
)

CHAT_MAX_STEPS = _int_env("CHAT_MAX_STEPS", 30)



HISTORY_DIR = _str_env(
    "HISTORY_DIR", str(os.path.join(os.path.expanduser("~"), ".qlik-ai", "history"))
)

HISTORY_MAX = _int_env("HISTORY_MAX", 200)


GLOSSARY_FILE = _str_env("GLOSSARY_FILE", "glossary.md")



OPENAI_BASE_URL = _str_env("OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_API_KEY = _str_env("OPENAI_API_KEY")
OPENAI_MODEL = _str_env("OPENAI_MODEL", "gpt-4o-mini")

CHAT_MODEL = _str_env("CHAT_MODEL") or OPENAI_MODEL
OPENAI_ORGANISATION = _str_env("OPENAI_ORGANISATION")

OPENAI_TIMEOUT = _float_env("OPENAI_TIMEOUT", 600.0)

OPENAI_CONNECT_TIMEOUT = _float_env("OPENAI_CONNECT_TIMEOUT", 10.0)

OPENAI_VERIFY_SSL = _bool_env("OPENAI_VERIFY_SSL", True)


def _headers_env(name):
    raw = _str_env(name)
    headers = {}
    for part in raw.replace("\n", ";").split(";"):
        if not part.strip():
            continue
        if ":" not in part:
            raise ConfigError(
                f"{name} entries must be 'Header: value', got {part.strip()!r}"
            )
        key, value = part.split(":", 1)
        headers[key.strip()] = value.strip()
    return headers


OPENAI_EXTRA_HEADERS = _headers_env("OPENAI_EXTRA_HEADERS")


def _json_env(name):
    raw = _str_env(name)
    if not raw:
        return {}
    import json

    try:
        value = json.loads(raw)
    except ValueError as e:
        raise ConfigError(f"{name} must be a JSON object: {e}")
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a JSON object, got {type(value).__name__}")
    return value


OPENAI_EXTRA_BODY = _json_env("OPENAI_EXTRA_BODY")


LDAP_ENABLED = _bool_env("LDAP_ENABLED", False)

LDAP_SERVER = _str_env("LDAP_SERVER")
LDAP_USE_SSL = _bool_env("LDAP_USE_SSL", True)
LDAP_START_TLS = _bool_env("LDAP_START_TLS", False)
LDAP_PORT = _int_env("LDAP_PORT", 636 if LDAP_USE_SSL else 389)

LDAP_UPN_SUFFIX = _str_env("LDAP_UPN_SUFFIX")
LDAP_WINDOWS_DOMAIN = _str_env("LDAP_WINDOWS_DOMAIN")

LDAP_BASE_DN = _str_env("LDAP_BASE_DN")
LDAP_USER_ATTRIBUTE = _str_env("LDAP_USER_ATTRIBUTE", "sAMAccountName")

LDAP_BIND_USER = _str_env("LDAP_BIND_USER")
LDAP_BIND_PASSWORD = _str_env("LDAP_BIND_PASSWORD")

LDAP_ADMIN_GROUP = _str_env("LDAP_ADMIN_GROUP")

LDAP_QLIK_DIRECTORY = _str_env("LDAP_QLIK_DIRECTORY") or LDAP_WINDOWS_DOMAIN

LDAP_TIMEOUT = _float_env("LDAP_TIMEOUT", 10.0)

if LDAP_ENABLED:
    _missing = [name for name, value in (
        ("LDAP_SERVER", LDAP_SERVER),
        ("LDAP_BASE_DN", LDAP_BASE_DN),
    ) if not value]
    if _missing:
        raise ConfigError(
            "LDAP_ENABLED is on but " + " and ".join(_missing) + " "
            + ("is" if len(_missing) == 1 else "are") + " not set."
        )
    if not (LDAP_UPN_SUFFIX or LDAP_WINDOWS_DOMAIN):
        raise ConfigError(
            "LDAP_ENABLED is on but neither LDAP_UPN_SUFFIX nor "
            "LDAP_WINDOWS_DOMAIN is set - there is no way to turn a typed "
            "username into something Active Directory will accept."
        )
    if not LDAP_QLIK_DIRECTORY:
        raise ConfigError(
            "LDAP_ENABLED is on but there is no Qlik user directory to give "
            "the people who sign in. Set LDAP_QLIK_DIRECTORY to whatever the "
            "QMC calls this directory, or set LDAP_WINDOWS_DOMAIN and it will "
            "be used."
        )


USERS_DB = _str_env(
    "USERS_DB", str(os.path.join(os.path.expanduser("~"), ".qlik-ai", "users.db"))
)

AUTH_ENABLED = _bool_env("AUTH_ENABLED", True)
ALLOW_SHARED_QLIK_IDENTITY = _bool_env("ALLOW_SHARED_QLIK_IDENTITY", False)

SESSION_COOKIE = _str_env("SESSION_COOKIE", "qlikai_session")
SESSION_HOURS = _int_env("SESSION_HOURS", 12)
COOKIE_SECURE = _bool_env("COOKIE_SECURE", False)

ADMIN_USERNAME = _str_env("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = _str_env("ADMIN_PASSWORD")

PASSWORD_MIN = max(1, _int_env("PASSWORD_MIN", 1))
PBKDF2_ROUNDS = _int_env("PBKDF2_ROUNDS", 240_000)

LOGIN_MAX_ATTEMPTS = _int_env("LOGIN_MAX_ATTEMPTS", 5)
LOGIN_LOCKOUT_MINUTES = _int_env("LOGIN_LOCKOUT_MINUTES", 15)

MAX_USER_SESSIONS = _int_env("MAX_USER_SESSIONS", 200)
USER_SESSION_IDLE_MINUTES = _int_env("USER_SESSION_IDLE_MINUTES", 60)


AUDIT_RETENTION_DAYS = _int_env("AUDIT_RETENTION_DAYS", 0)
HISTORY_RETENTION_DAYS = _int_env("HISTORY_RETENTION_DAYS", 0)


WORKER_THREADS = _int_env("WORKER_THREADS", 128)

LLM_MAX_CONNECTIONS = _int_env("LLM_MAX_CONNECTIONS", 200)
LLM_MAX_KEEPALIVE = _int_env("LLM_MAX_KEEPALIVE", 50)


LOG_FILE = _str_env("LOG_FILE")
LOG_LEVEL = _str_env("LOG_LEVEL", "INFO").upper()

LOG_MAX_MB = _int_env("LOG_MAX_MB", 20)
LOG_BACKUPS = _int_env("LOG_BACKUPS", 10)


def summary():
    lines = [
        f"mode              {QLIK_MODE}",
        f"qlik              {QLIK_HOST}:{QLIK_PORT}",
        f"app               {APP_NAME!r}",
        f"timeouts          connect {QLIK_CONNECT_TIMEOUT}s / request {QLIK_REQUEST_TIMEOUT}s",
    ]

    lines += [
        f"model endpoint    {OPENAI_BASE_URL}",
        f"model             {OPENAI_MODEL}",
        f"api key           {'set' if OPENAI_API_KEY else '(not set)'}",
    ]

    lines += [
        f"sign-in           {'required' if AUTH_ENABLED else 'OFF - loopback only'}",
        f"accounts          {USERS_DB}",
        f"conversations     {HISTORY_DIR}",
        f"request threads   {WORKER_THREADS}",
    ]

    if LDAP_ENABLED:
        security = ("LDAPS" if LDAP_USE_SSL else
                    "StartTLS" if LDAP_START_TLS else "NO TLS")
        lines += [
            f"directory         {LDAP_SERVER}:{LDAP_PORT} ({security})",
            f"  base dn         {LDAP_BASE_DN}",
            f"  qlik directory  {LDAP_QLIK_DIRECTORY or '(not set)'}",
            f"  admin group     {LDAP_ADMIN_GROUP or '(roles stay local)'}",
        ]
    else:
        lines.append("directory         off - accounts are local to this server")
    if QLIK_MODE == ENTERPRISE:
        identity = (
            f"{QLIK_USER_DIRECTORY}\\{QLIK_USER_ID}"
            if QLIK_USER_DIRECTORY and QLIK_USER_ID
            else "(not set)"
        )
        lines += [
            f"cert dir          {QLIK_CERT_DIR or '(not set)'}",
            f"engine identity   {identity}",
            f"tls verify        {QLIK_SSL_VERIFY}",
        ]
    return "\n".join(lines)
