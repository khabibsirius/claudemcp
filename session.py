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

import history
from chat_tools import system_prompt
from config import APP_NAME, CHAT_MODEL, OLLAMA_HOST
from ollama_client import OllamaError, pick_tool_model
from qlik_engine import QlikConnectionError, QlikEngine, QlikNotConnectedError

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
    # The saved conversation these messages belong to. Minted on the first
    # reset or the first save, so a server that is never chatted to writes
    # nothing to disk.
    "chat_id": None,
    # Whether the assistant may run a reload itself. On by default: with it
    # off, "load my data and chart it" stalls halfway and the model reports
    # that reloading is impossible rather than that it needs a click.
    "allow_reload": True,
    # Which language the assistant answers in. "" lets it follow whatever
    # the question was written in, which is the right default until someone
    # says otherwise.
    "language": "",
}


def allow_reload():
    return _state["allow_reload"]


def language():
    return _state["language"]


def set_language(code):
    """Pin the assistant's language, including for the conversation open now.

    The system prompt is written once when a conversation starts, so without
    rewriting it here the setting would not take effect until the next chat -
    which reads as the switch being broken.
    """
    with _lock:
        _state["language"] = (code or "").strip().lower()
        messages_ = _state["messages"]
        if messages_ and messages_[0].get("role") == "system":
            messages_[0]["content"] = system_prompt(_state["language"])


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


def list_apps():
    """Every app on this Qlik, whether one is open or not.

    Listing needs a connection, not an open document, but the picker used to
    ask through engine() - which refuses when nothing is open - and fell back
    to showing the single app it already had. With one entry in the list
    there was nothing to switch TO, so the app could not be changed at all
    from a session that had lost its document.
    """
    with _lock:
        if connected():
            return [a["name"] for a in _state["engine"].list_apps() if a["name"]]

        # Only when there is no connection to borrow: Qlik Sense Desktop
        # allows one session per app, so a second socket alongside a live
        # one is exactly what this module exists to avoid.
        with QlikEngine() as scratch:
            return [a["name"] for a in scratch.list_apps() if a["name"]]


def _teardown_engine():
    """Back to "nothing open", without letting a failed close hide why."""
    engine_ = _state["engine"]
    if engine_ is not None:
        try:
            engine_.close()
        except Exception:
            log.warning("Could not close the engine cleanly", exc_info=True)
    _state["engine"] = None
    _state["app_name"] = None


def open_app(app_name=None):
    """Open an app. Changing app starts a new session, because it must.

    Qlik Sense Desktop keeps ONE document open per engine, and the engine
    offers no way to close one - there is no CloseDoc method. The document
    is released when the session holding it disconnects, and not before, so
    a session that keeps the current app open makes every other app
    unopenable: the engine refuses with "a document is already open" (1002),
    which reads as though the app being asked for is the problem. Holding
    the connection across a switch is what made the app impossible to
    change; the socket goes first, and the new app opens on one of its own.
    """
    app_name = app_name or APP_NAME

    with _lock:
        if connected() and _state["app_name"] == app_name:
            return _state["engine"]

        previous = _state["app_name"] if connected() else None
        if previous is not None:
            # Filed while the app it was asked against is still the open one,
            # so the chat is not recorded against the app replacing it.
            reset_chat()
            _teardown_engine()

        if not connected():
            _state["engine"] = QlikEngine()

        try:
            _state["engine"].open_app(app_name)
        except Exception:
            _teardown_engine()
            if previous is not None and previous != app_name:
                _reopen(previous)
            raise

        _state["app_name"] = app_name
        if previous is None:
            # Nothing was filed above, so this is the first app of the
            # session: whatever was said before it was open was not about it.
            reset_chat()
        return _state["engine"]


def _reopen(app_name):
    """Go back to the app that was open before a switch failed.

    The old session had to be dropped to try the new app at all, so failing
    without this would leave nothing open at all - a worse place than the
    person started from, for asking for an app they could not have.
    """
    try:
        _state["engine"] = QlikEngine()
        _state["engine"].open_app(app_name)
        _state["app_name"] = app_name
    except Exception:
        log.warning("Could not reopen %r after a failed switch", app_name,
                    exc_info=True)
        _teardown_engine()


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
    # The lock, like every other _state write: two first requests arriving
    # together would otherwise each install their own client.
    with _lock:
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


def chat_id():
    return _state["chat_id"]


def persist():
    """Write the open conversation to disk. Never fatal to a chat turn.

    An unwritable history directory should cost the user their history, not
    the answer they are waiting for - so a failure here is logged and the
    turn still returns.
    """
    with _lock:
        if _state["chat_id"] is None:
            _state["chat_id"] = history.new_id()
        try:
            return history.save(_state["chat_id"], _state["messages"], _state["app_name"])
        except Exception:
            log.exception("Could not save chat history")
            return None


def reset_chat():
    """Start a new conversation, keeping the one being replaced.

    Also reached by open_app(): switching apps ends the chat, and the old one
    is worth keeping even though nothing asked for it to be saved.
    """
    with _lock:
        if _state["chat_id"] is not None:
            persist()
        _state["chat_id"] = history.new_id()
        _state["messages"] = [{"role": "system", "content": system_prompt(_state["language"])}]
    return _state["messages"]


def load_chat(chat_id_):
    """Reopen a saved conversation as the live one.

    The saved messages become the model's context again, not just the
    transcript on screen - otherwise the assistant would remember nothing of
    the chat the user is looking at.
    """
    record = history.load(chat_id_)
    if record is None:
        return None

    with _lock:
        if _state["chat_id"] is not None and _state["chat_id"] != chat_id_:
            persist()
        # The saved answers are about the app they were asked against.
        # Restoring them on top of a different open app leaves the model
        # reasoning about the wrong data model, so switch back first - the
        # reset that open_app does is overwritten just below.
        wanted = record.get("app")
        if wanted and _state["app_name"] and wanted != _state["app_name"]:
            try:
                open_app(wanted)
            except Exception as e:
                # Still load the chat - losing the transcript would be worse -
                # but tell the caller instead of silently proceeding.
                log.warning("Could not reopen app %r for chat %s: %s",
                            wanted, chat_id_, e)
                record["app_mismatch"] = (
                    f"This chat is about the app {wanted!r}, which could not "
                    f"be reopened: {e}"
                )
        messages_ = record["messages"]
        # A history file written before the prompt changed - or hand-edited -
        # still has to start with the rules the tools are described by.
        if not messages_ or messages_[0].get("role") != "system":
            messages_ = [{"role": "system", "content": system_prompt(_state["language"])}] + messages_
        _state["messages"] = messages_
        _state["chat_id"] = chat_id_
    return record


def delete_chat(chat_id_):
    """Remove a saved chat, starting a new one if it was the open chat."""
    with _lock:
        history.delete(chat_id_)
        if _state["chat_id"] == chat_id_:
            _state["chat_id"] = history.new_id()
            _state["messages"] = [{"role": "system", "content": system_prompt(_state["language"])}]
    return True
