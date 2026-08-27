"""Settings, read from the environment / .env (see .env.example).

This module only reads and validates configuration - it never opens a
connection. A bad value fails here, at import, with a message naming the
setting, instead of surfacing later as an opaque socket or SSL error.
"""

import os

from dotenv import load_dotenv

load_dotenv()


class ConfigError(Exception):
    """Raised when a setting is missing or malformed."""


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


# ----------------------------------------------------------------------
# Qlik
# ----------------------------------------------------------------------

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

# Guards the two ways an engine call can hang: the host not listening at all,
# and the host accepting the socket but never answering.
QLIK_CONNECT_TIMEOUT = _float_env("QLIK_CONNECT_TIMEOUT", 10.0)
QLIK_REQUEST_TIMEOUT = _float_env("QLIK_REQUEST_TIMEOUT", 60.0)

# A dropped connection is rebuilt on the next request, but not faster than
# this. When Qlik is down every request from every user would otherwise try
# to reconnect, each one waiting out QLIK_CONNECT_TIMEOUT while holding a
# worker thread - which is how an unreachable Qlik takes this server down
# with it. Inside the window the last real reason is repeated instead.
#
# Pressing Reconnect ignores this: a person can see that Qlik is back, and
# there is only one of them.
RECONNECT_COOLDOWN_SECONDS = _float_env("RECONNECT_COOLDOWN_SECONDS", 15.0)

# Whether a Qlik that is unreachable at startup stops the server coming up.
# It should not, and it no longer does: the login page, the admin pages,
# saved conversations and the audit trail have nothing to do with Qlik, and
# taking all of them away because one dependency is restarting turns a Qlik
# outage into a total one. Set true only to fail fast in a deployment where
# a half-working server is worse than none.
REQUIRE_QLIK_AT_STARTUP = _bool_env("REQUIRE_QLIK_AT_STARTUP", False)

# Enterprise on-premise only: a direct, certificate-authenticated connection
# to the Engine API that bypasses the proxy. Validated in QlikEngine.connect()
# rather than here, so desktop users are never asked for certificates.
QLIK_CERT_DIR = _str_env("QLIK_CERT_DIR")
QLIK_USER_DIRECTORY = _str_env("QLIK_USER_DIRECTORY")
QLIK_USER_ID = _str_env("QLIK_USER_ID")
QLIK_SSL_VERIFY = _bool_env("QLIK_SSL_VERIFY", True)

CLIENT_CERT = "client.pem"
CLIENT_KEY = "client_key.pem"
ROOT_CERT = "root.pem"


# ----------------------------------------------------------------------
# Local LLM
# ----------------------------------------------------------------------

OLLAMA_MODEL = _str_env("OLLAMA_MODEL", "phi4:14b")
# Blank means "let the ollama package use its own default host".
OLLAMA_HOST = _str_env("OLLAMA_HOST")
# Generous on purpose: a large reasoning model thinks before it answers, and
# a timeout that fires mid-generation is indistinguishable from a hang to the
# person watching.
OLLAMA_TIMEOUT = _float_env("OLLAMA_TIMEOUT", 600.0)

# The context window (in tokens) requested from Ollama on every call. Left
# unset, Ollama uses its own default (4,096 for most installs) and silently
# drops the OLDEST part of an oversized prompt - which is the system prompt
# carrying all the rules. That doesn't error; it lobotomises the assistant
# mid-conversation, which reads as "the model is stupid" when it is actually
# blindfolded. Raise this as far as the machine's memory allows - a large
# model on a dedicated server is comfortable at 32768 or 65536. The old
# default of 16384 was smaller than CHAT_HISTORY_RESERVE below - the whole
# window could not hold the system prompt, tool schemas and a full reply.
OLLAMA_NUM_CTX = _int_env("OLLAMA_NUM_CTX", 32768)

# Character budget for the chat history sent to the model, derived from the
# context window unless set explicitly: roughly 3 characters per token, minus
# room for the system prompt, tool schemas and the reply. The old fixed
# 24,000 was sized for a 7B model and gave a 128k-context model ~6k tokens of
# working memory - it forgot the user's request two tool calls in.
#
# The reserve is measured, not guessed: SYSTEM_PROMPT is ~6,300 tokens and the
# tool schemas ~2,100, so ~8,300 is spent before the conversation starts. The
# old 6,000 reserve was set when the prompt was a quarter of its current size
# and now overflows the window on its own, which Ollama resolves by silently
# truncating the FRONT - deleting the system prompt and its rules mid-chat.
#
# The rest is for the reply, and the reply is the reason this is not
# tighter. An inventory briefing lists EVERY chart on its own numbered row
# with its own figures - a 34-chart app is 34 rows plus an overall picture,
# which is 5,000 tokens of output. Sized so that finishes rather than being
# cut off mid-table, which is the worst possible outcome: a table that stops
# at row 19 looks like the app only has 19 charts.
#
# 18,000 leaves ~14,700 tokens of history - roughly 20 turns, which is still
# more conversation than a working session uses.
#
# This has been raised four times as the prompt grew - most recently when
# the worked examples and the numbers/language sections were merged into
# one prompt. It is now more than half a 32k window, and the next increase
# should be a decision to TRIM the prompt instead: past this point the
# assistant is spending more context on its instructions than on the
# user's conversation.
#
# Fifth raise, 19,000 -> 19,500, and taken as that decision rather than
# around it. The prompt was trimmed four times first - the inventory
# briefing, the capability rules and the numbers section were all tightened
# and the reserve still would not hold what had to go in. What forced it was
# a real gap rather than more prose: the router had no DELETE intent, so the
# assistant told a user it could not delete sheets while holding
# delete_sheet. Denying a capability it has is the same defect as inventing
# one, and closing it cost more than the trimming had freed.
#
# The 500 costs ~1,500 characters of history, about 3.6%. If this needs
# raising again, the question to ask first is whether OLLAMA_NUM_CTX can go
# up instead - the prompt is near the point where trimming it further starts
# removing rules that exist because something went wrong once.
CHAT_HISTORY_RESERVE = 19_500

CHAT_HISTORY_CHARS = _int_env(
    "CHAT_HISTORY_CHARS", max(24_000, (OLLAMA_NUM_CTX - CHAT_HISTORY_RESERVE) * 3)
)

# How many tool calls the assistant may make for one request. A real
# "load these files, clean them and chart them" flow is 15-25 calls; the old
# cap of 12 cut it off halfway through.
CHAT_MAX_STEPS = _int_env("CHAT_MAX_STEPS", 30)

# chat.py needs a model that supports tool calling, which many good models
# do not - phi4 and deepseek-coder among them. Kept separate from
# OLLAMA_MODEL so the dashboard designer, which only needs JSON output, can
# keep using a model that has no tool support.
CHAT_MODEL = _str_env("CHAT_MODEL") or OLLAMA_MODEL


# ----------------------------------------------------------------------
# Saved conversations
# ----------------------------------------------------------------------

# Under the user's home rather than the project, so history survives moving
# or re-cloning the checkout and never lands in a commit by accident.
HISTORY_DIR = _str_env(
    "HISTORY_DIR", str(os.path.join(os.path.expanduser("~"), ".qlik-ai", "history"))
)

# The sidebar is a flat list and building it reads every file, so the count
# is capped. 0 disables pruning for anyone who would rather keep the lot.
HISTORY_MAX = _int_env("HISTORY_MAX", 200)


# ----------------------------------------------------------------------
# Glossary
# ----------------------------------------------------------------------

# The institution's own definitions - fiscal year, what a ratio is made of -
# read at the start of every conversation. Absent by default: most installs
# never write one.
GLOSSARY_FILE = _str_env("GLOSSARY_FILE", "glossary.md")


# ----------------------------------------------------------------------
# Which model provider
# ----------------------------------------------------------------------

# "ollama"  - a model on this machine. Nothing leaves the network at all,
#             which is the strongest data-protection story available and the
#             reason this was the only option for so long.
# "openai"  - any endpoint speaking OpenAI's /v1/chat/completions: OpenAI,
#             Azure OpenAI, OpenRouter, Together, Groq, or a vLLM or TGI
#             server inside your own network. The last of those keeps the
#             data-protection story and drops the four-minute answers.
#
# This setting existed and was read by nothing, so setting it changed no
# behaviour and the local model kept being used. It is wired up now.
LLM_PROVIDER = _str_env("LLM_PROVIDER", "ollama").lower()

# The API root, up to and including the version prefix.
#   OpenAI        https://api.openai.com/v1
#   OpenRouter    https://openrouter.ai/api/v1
#   vLLM / TGI    http://your-host:8000/v1
#   Azure OpenAI  https://<resource>.openai.azure.com/openai/deployments/<deployment>
OPENAI_BASE_URL = _str_env("OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_API_KEY = _str_env("OPENAI_API_KEY")
OPENAI_MODEL = _str_env("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_ORGANISATION = _str_env("OPENAI_ORGANISATION")

# Generous, for the same reason OLLAMA_TIMEOUT is: a long answer with several
# tool calls is minutes of wall clock, and a timeout that fires mid-generation
# is indistinguishable from a hang.
OPENAI_TIMEOUT = _float_env("OPENAI_TIMEOUT", 600.0)

# Reaching the endpoint is a different wait from waiting for it to think, and
# only one of them deserves ten minutes. A firewall that drops packets rather
# than refusing them leaves a connection attempt hanging for the full budget,
# and every one of those holds a worker thread and the asking user's session
# lock - so an unreachable model server was able to occupy all 128 threads
# and stall the site for people who were not even using the assistant.
#
# Ten seconds is the honest answer to "is this endpoint there?". The long
# budget still applies to generation, which is what it was for.
OPENAI_CONNECT_TIMEOUT = _float_env("OPENAI_CONNECT_TIMEOUT", 10.0)

# Turn off only for an endpoint inside your own network using a private CA.
# It stops the server being authenticated, so it is a decision, not a default.
OPENAI_VERIFY_SSL = _bool_env("OPENAI_VERIFY_SSL", True)


def _headers_env(name):
    """Extra headers as `Key: value` pairs separated by newlines or semicolons.

    OpenRouter asks for HTTP-Referer and X-Title; a corporate gateway may want
    a routing header. Rather than a setting per provider, one place to put
    whatever a particular endpoint insists on.
    """
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
    """A JSON object merged into every request body.

    The escape hatch for whatever a particular endpoint wants that is not in
    the standard. Qwen served by vLLM or DashScope takes

        {"chat_template_kwargs": {"enable_thinking": false}}

    which stops the reasoning monologue being generated at all, rather than
    it being stripped after the fact - cheaper and faster than paying for
    tokens nobody reads.
    """
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


# ----------------------------------------------------------------------
# Active Directory sign-in
# ----------------------------------------------------------------------

# On means people sign in with the Windows username and password they
# already use for Qlik Sense, checked against a domain controller. Their
# account here creates itself on first sign-in, with the Qlik identity
# already filled in from AD - which is the difference between a rollout and
# an afternoon of typing user directories for three hundred people.
#
# Local accounts keep working alongside it, so there is always a way in when
# the directory is unreachable.
LDAP_ENABLED = _bool_env("LDAP_ENABLED", False)

LDAP_SERVER = _str_env("LDAP_SERVER")           # dc01.bank.internal
LDAP_USE_SSL = _bool_env("LDAP_USE_SSL", True)  # LDAPS
LDAP_START_TLS = _bool_env("LDAP_START_TLS", False)
LDAP_PORT = _int_env("LDAP_PORT", 636 if LDAP_USE_SSL else 389)

# What users type in front of their name, and what AD knows them as.
# The UPN suffix is preferred: jsmith@bank.internal works across a forest
# with several domains, where the NetBIOS name does not.
LDAP_UPN_SUFFIX = _str_env("LDAP_UPN_SUFFIX")       # bank.internal
LDAP_WINDOWS_DOMAIN = _str_env("LDAP_WINDOWS_DOMAIN")   # BANK

# Where to search for the user, and on which attribute. sAMAccountName is
# what Qlik's own AD connector uses as the user id, so matching on it keeps
# the two systems naming the same person the same way.
LDAP_BASE_DN = _str_env("LDAP_BASE_DN")             # DC=bank,DC=internal
LDAP_USER_ATTRIBUTE = _str_env("LDAP_USER_ATTRIBUTE", "sAMAccountName")

# Only needed where ordinary users may not read their own directory entry.
# Left blank, the search runs as the person who just signed in.
LDAP_BIND_USER = _str_env("LDAP_BIND_USER")
LDAP_BIND_PASSWORD = _str_env("LDAP_BIND_PASSWORD")

# Members of this group administer the assistant, so joiners and leavers are
# handled where the bank already handles them. Blank leaves roles local.
# Full DN: CN=Qlik AI Admins,OU=Groups,DC=bank,DC=internal
LDAP_ADMIN_GROUP = _str_env("LDAP_ADMIN_GROUP")

# What Qlik calls the user directory these people came from. Usually the
# NetBIOS domain, but it is the name of the QMC's connector rather than a
# fact about AD - and getting it wrong opens engine sessions as a user the
# QMC has never heard of, which surfaces as a permissions error rather than
# as a configuration one.
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
        # Refused rather than allowed to degrade. Without it every account
        # created from the directory gets an empty Qlik identity, which does
        # not fail - it silently drops all of them onto the server's shared
        # connection and the one global lock, so they queue behind each
        # other. Nothing on screen says why; it surfaces only as "the
        # assistant is slow", months later and for everybody at once.
        raise ConfigError(
            "LDAP_ENABLED is on but there is no Qlik user directory to give "
            "the people who sign in. Set LDAP_QLIK_DIRECTORY to whatever the "
            "QMC calls this directory, or set LDAP_WINDOWS_DOMAIN and it will "
            "be used."
        )


# ----------------------------------------------------------------------
# Accounts and access
# ----------------------------------------------------------------------

# Beside the history rather than in the project, for the same reasons: it
# survives re-cloning the checkout and cannot be committed by accident.
USERS_DB = _str_env(
    "USERS_DB", str(os.path.join(os.path.expanduser("~"), ".qlik-ai", "users.db"))
)

# Off is for a single-operator desktop install and nothing else. It is
# checked again in web_app.main(), which refuses to bind to anything but
# loopback while it is off - the failure mode this guards is somebody
# running --host 0.0.0.0 so a colleague can try it, which puts the bank's
# figures and a reload button on the network with no login at all.
AUTH_ENABLED = _bool_env("AUTH_ENABLED", True)

SESSION_COOKIE = _str_env("SESSION_COOKIE", "qlikai_session")
SESSION_HOURS = _int_env("SESSION_HOURS", 12)
# Set this once the server is behind HTTPS. It cannot default to true: a
# Secure cookie is never sent over http, so the login would appear to
# succeed and every following request would arrive logged out.
COOKIE_SECURE = _bool_env("COOKIE_SECURE", False)

# The first administrator, created on first run when there are no accounts.
# With no password set, one is generated and printed once at startup.
ADMIN_USERNAME = _str_env("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = _str_env("ADMIN_PASSWORD")

PASSWORD_MIN = _int_env("PASSWORD_MIN", 8)
# OWASP's 2023 floor for PBKDF2-HMAC-SHA256 is 600,000; this is lower on
# purpose, because it runs on the same box as a 22 GB model and is paid on
# every login. Raise it on a machine that is not also serving the LLM -
# stored hashes record their own cost, so old passwords keep working.
PBKDF2_ROUNDS = _int_env("PBKDF2_ROUNDS", 240_000)

LOGIN_MAX_ATTEMPTS = _int_env("LOGIN_MAX_ATTEMPTS", 5)
LOGIN_LOCKOUT_MINUTES = _int_env("LOGIN_LOCKOUT_MINUTES", 15)

# How many per-user sessions to keep before looking for idle ones to close.
# A soft cap, not a hard one: only sessions idle longer than the timeout
# below are closed, so a server genuinely busy with more people than this
# keeps them all rather than evicting somebody mid-question. Each session
# holds a Qlik socket in enterprise mode, and an engine session nobody is
# using is still an open document and a licence seat on the server.
MAX_USER_SESSIONS = _int_env("MAX_USER_SESSIONS", 200)
USER_SESSION_IDLE_MINUTES = _int_env("USER_SESSION_IDLE_MINUTES", 60)


# ----------------------------------------------------------------------
# Retention
# ----------------------------------------------------------------------

# How long to keep records that are about people rather than about the
# software. Both hold customer figures by implication - a conversation
# contains whatever somebody asked about the bank's data, and the audit
# trail records who asked - so an institution will have a policy about them
# even if this software does not.
#
# 0 keeps them forever, which is the default because silently deleting a
# bank's audit trail is a worse failure than keeping too much. Set them to
# whatever the policy says, and they are enforced at startup and by
# `manage_users.py prune`.
AUDIT_RETENTION_DAYS = _int_env("AUDIT_RETENTION_DAYS", 0)
HISTORY_RETENTION_DAYS = _int_env("HISTORY_RETENTION_DAYS", 0)


# ----------------------------------------------------------------------
# Concurrency
# ----------------------------------------------------------------------

# How many requests can be in flight at once.
#
# Every endpoint in web_app.py is a plain `def`, which Starlette runs in a
# worker thread - and a chat turn holds its thread for the whole turn, tool
# calls and model latency included. anyio's default pool is FORTY threads,
# so the forty-first concurrent chat does not merely queue: it blocks the
# whole server, login page included, until one finishes.
#
# Raising it is safe here because the work is I/O-bound - a websocket to
# Qlik and an HTTPS call to the model - so the threads are asleep almost all
# the time and the GIL is not the constraint. Threads are pooled and created
# on demand, so a high ceiling costs nothing until it is used.
#
# Size it above the number of people who might be mid-question at once, not
# above the number of accounts. 300 users of whom 30 are asking needs ~64;
# 300 genuinely simultaneous needs ~400.
WORKER_THREADS = _int_env("WORKER_THREADS", 128)

# Sockets the model client keeps open. One shared pool across all users, so
# TLS handshakes are paid once rather than per person. Must be at least as
# large as the number of chat turns that can run at once, or turns queue on
# the connection pool instead of on the model.
LLM_MAX_CONNECTIONS = _int_env("LLM_MAX_CONNECTIONS", 200)
LLM_MAX_KEEPALIVE = _int_env("LLM_MAX_KEEPALIVE", 50)


# ----------------------------------------------------------------------
# Logging
# ----------------------------------------------------------------------

# Where the server writes its own log. Blank means the console, which is
# right when somebody is watching it and useless once it runs as a service -
# a Windows service has no console, so without this the only record of a
# problem is whatever the service wrapper happened to catch.
#
# This is the OPERATIONAL log: what the server did and what went wrong.
# Who did what is the audit trail in the accounts database, which is a
# different thing with different retention needs.
LOG_FILE = _str_env("LOG_FILE")
LOG_LEVEL = _str_env("LOG_LEVEL", "INFO").upper()

# Rotation, so a long-running service cannot fill the disk. Ten files of
# 20 MB is a couple of weeks on a busy server and a few months on a quiet one.
LOG_MAX_MB = _int_env("LOG_MAX_MB", 20)
LOG_BACKUPS = _int_env("LOG_BACKUPS", 10)


def summary():
    """Human-readable settings dump for smoke tests and error messages.

    Deliberately reports only whether the enterprise identity is set, not
    what it is.
    """
    lines = [
        f"mode              {QLIK_MODE}",
        f"qlik              {QLIK_HOST}:{QLIK_PORT}",
        f"app               {APP_NAME!r}",
        f"timeouts          connect {QLIK_CONNECT_TIMEOUT}s / request {QLIK_REQUEST_TIMEOUT}s",
    ]

    if LLM_PROVIDER == "ollama":
        lines += [
            f"model provider    ollama (local)",
            f"ollama model      {OLLAMA_MODEL}",
            f"ollama host       {OLLAMA_HOST or '(package default)'}",
        ]
    else:
        lines += [
            f"model provider    {LLM_PROVIDER} (OpenAI-compatible)",
            f"model endpoint    {OPENAI_BASE_URL}",
            f"model             {OPENAI_MODEL}",
            # Whether, never what.
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
