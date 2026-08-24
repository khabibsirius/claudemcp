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
# model on a dedicated server is comfortable at 32768 or 65536.
OLLAMA_NUM_CTX = _int_env("OLLAMA_NUM_CTX", 16384)

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
# This has been raised three times as the prompt grew. It is now more than
# half the window, and the next increase should be a decision to TRIM the
# prompt instead: past this point the assistant is spending more context on
# its instructions than on the user's conversation.
CHAT_HISTORY_RESERVE = 18_000

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
        f"ollama model      {OLLAMA_MODEL}",
        f"ollama host       {OLLAMA_HOST or '(package default)'}",
    ]
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
