"""The single Qlik session and model that everything shares.

The web UI, the assistant and the MCP tools all work on one open app rather
than each opening their own. That is what lets the same process serve a
browser and an MCP client at once without them fighting over the document -
Qlik Sense Desktop allows only one session per app anyway, so two connections
would simply lock each other out.
"""

import logging
import threading

import ollama

from chat_tools import SYSTEM_PROMPT
from config import APP_NAME, CHAT_MODEL, OLLAMA_HOST
from ollama_client import OllamaError, pick_tool_model
from qlik_engine import QlikEngine, QlikNotConnectedError

log = logging.getLogger(__name__)

# RLock: opening an app resets the conversation, so these nest.
_lock = threading.RLock()

_state = {
    "engine": None,
    "app_name": None,
    # None until a model has been resolved *and* confirmed to support tools.
    # Seeding this with the configured name made assistant_ready() true while
    # Ollama was down, so requests sailed past the check and failed later as
    # a raw connection error.
    "model": None,
    "client": None,
    "messages": [],
    # Whether the assistant may run a reload itself. On by default: with it
    # off, "load my data and chart it" stalls halfway and the model reports
    # that reloading is impossible rather than that it needs a click.
    "allow_reload": True,
}


def allow_reload():
    return _state["allow_reload"]


def set_allow_reload(value):
    with _lock:
        _state["allow_reload"] = bool(value)
    return _state["allow_reload"]


def lock():
    """The shared lock, for callers that need several steps to be atomic."""
    return _lock


# ----------------------------------------------------------------------
# Qlik
# ----------------------------------------------------------------------

def connected():
    return _state["engine"] is not None and _state["engine"].connected


def engine():
    """The open engine, or a clear error saying what to do about it."""
    if not connected():
        raise QlikNotConnectedError("No app is open. Open one first.")
    return _state["engine"]


def open_app(app_name=None):
    """Open an app, reusing the existing connection where possible."""
    app_name = app_name or APP_NAME

    with _lock:
        if connected() and _state["app_name"] == app_name:
            return _state["engine"]

        if not connected():
            _state["engine"] = QlikEngine()

        try:
            _state["engine"].open_app(app_name)
        except Exception:
            # A half-open session is worse than none: every later call would
            # fail against a document that was never opened.
            if _state["engine"] is not None and _state["app_name"] is None:
                _state["engine"].close()
                _state["engine"] = None
            raise

        _state["app_name"] = app_name
        # Whatever was said before was about a different app's fields.
        reset_chat()
        return _state["engine"]


def ensure_open():
    """Open the configured app if nothing is open yet."""
    if not connected():
        open_app(APP_NAME)
    return _state["engine"]


def app_name():
    return _state["app_name"]


def close():
    with _lock:
        if _state["engine"] is not None:
            _state["engine"].close()
        _state["engine"] = None
        _state["app_name"] = None


# ----------------------------------------------------------------------
# Model
# ----------------------------------------------------------------------

def client():
    if _state["client"] is None:
        _state["client"] = ollama.Client(host=OLLAMA_HOST or None)
    return _state["client"]


def model():
    return _state["model"]


def set_model(name):
    from ollama_client import supports_tools

    if not supports_tools(client(), name):
        raise OllamaError(f"{name} cannot call tools, so it can't run the assistant.")
    with _lock:
        _state["model"] = name
    return name


def resolve_model(preferred=None):
    """Settle on a model that can drive the assistant, and remember it."""
    chosen, note = pick_tool_model(client(), preferred or _state["model"] or CHAT_MODEL)
    if not chosen:
        raise OllamaError("No usable model.")
    with _lock:
        _state["model"] = chosen
    return chosen, note


def assistant_ready():
    """Whether there is a usable model. False when Ollama isn't running."""
    return bool(_state["model"])


def ensure_model():
    """Resolve a model now if we don't have one yet.

    Called on each assistant request so Ollama can be started *after* this
    server, and simply begin working - rather than the whole product refusing
    to open because one of its two dependencies was down at startup.
    """
    if _state["model"]:
        return _state["model"]

    # Fresh client: the previous one cached a dead connection.
    with _lock:
        _state["client"] = None
    return resolve_model(CHAT_MODEL)[0]


# ----------------------------------------------------------------------
# Conversation
# ----------------------------------------------------------------------

def messages():
    return _state["messages"]


def reset_chat():
    with _lock:
        _state["messages"] = [{"role": "system", "content": SYSTEM_PROMPT}]
    return _state["messages"]
