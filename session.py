import logging
import threading
import time

import history
from chat_tools import system_prompt
import llm
from config import (
    APP_NAME,
    ENTERPRISE,
    MAX_USER_SESSIONS,
    QLIK_MODE,
    RECONNECT_COOLDOWN_SECONDS,
    USER_SESSION_IDLE_MINUTES,
)
from llm import ModelError, pick_tool_model
from qlik_engine import QlikEngine, QlikEngineError, QlikNotConnectedError

log = logging.getLogger(__name__)

SYSTEM_KEY = "system"


class QlikConnectionLost(QlikNotConnectedError):
    pass
_engine_lock = threading.RLock()

_shared = {"engine": None}

_registry_lock = threading.RLock()
_sessions = {}


def per_user_engines():
    return QLIK_MODE == ENTERPRISE


class Session:
    def __init__(self, key, user=None):
        self.key = key
        user = user or {}
        self.user_id = user.get("id")
        self.username = user.get("username") or key
        self.qlik_directory = (user.get("qlik_directory") or "").strip()
        self.qlik_user_id = (user.get("qlik_user_id") or "").strip()

        self.owner = history.owner_key(self.user_id) if self.user_id else None

        self.touched = time.monotonic()

        self._displaced = False

        self._own_lock = threading.RLock()

        self._last_attempt = 0.0
        self._last_failure = None

        self.state = {
            "engine": None,
            "app_name": None,
            "model": None,
            "client": None,
            "messages": [],
            "chat_id": None,
            "allow_reload": True,
            "language": "",
        }

    def __repr__(self):
        return f"<Session {self.key} app={self.state['app_name']!r}>"

    @property
    def impersonates(self):
        return bool(per_user_engines() and self.qlik_directory and self.qlik_user_id)

    @property
    def shares_engine(self):
        return not self.impersonates

    @property
    def lock(self):
        return _engine_lock if self.shares_engine else self._own_lock

    def _new_engine(self):
        if self.impersonates:
            return QlikEngine(user_directory=self.qlik_directory,
                              user_id=self.qlik_user_id)
        return QlikEngine()

    def connected(self):
        return self.state["engine"] is not None and self.state["engine"].connected

    def engine(self):
        if not self.connected():
            if not self.state["app_name"]:
                raise QlikNotConnectedError("No app is open. Open one first.")
            self._recover()
        if self.shares_engine:
            self._ensure_document()
        return self.state["engine"]

    def _recover(self):
        with self.lock:
            if self.connected():
                return self.state["engine"]

            since = time.monotonic() - self._last_attempt
            if self._last_failure and since < RECONNECT_COOLDOWN_SECONDS:
                raise QlikConnectionLost(self._last_failure)

            self._last_attempt = time.monotonic()
            app_name = self.state["app_name"]
            try:
                engine_ = self._attach(app_name)
            except QlikEngineError as e:
                self._last_failure = (
                    f"The connection to Qlik was lost and could not be "
                    f"re-opened: {e}"
                )
                log.warning("Could not reconnect %s to %r: %s",
                            self.key, app_name, e)
                raise QlikConnectionLost(self._last_failure) from e

            log.info("Reconnected %s to %r", self.key, app_name)
            self._displaced = False
            self._last_failure = None
            return engine_

    def reconnect(self):
        with self.lock:
            self._last_attempt = 0.0
            self._last_failure = None
            engine_ = self.state["engine"]
            if engine_ is not None and not engine_.connected:
                self.state["engine"] = None
                self._close_quietly(engine_)
            if self.state["app_name"] is None:
                self.open_app(APP_NAME)
            elif not self.connected():
                self._recover()
            return self.state["engine"]

    @property
    def last_error(self):
        return self._last_failure

    def health(self):
        try:
            ready = self.assistant_ready()
        except Exception:
            ready = False
        return {
            "app": self.state["app_name"],
            "qlik_connected": self.connected(),
            "qlik_error": self._last_failure,
            "assistant_ready": ready,
            "model": self.state["model"],
        }

    def _ensure_document(self):
        wanted = self.state["app_name"]
        open_now = getattr(self.state["engine"], "app_name", None)
        if not wanted or open_now is None or open_now == wanted:
            return
        with _engine_lock:
            if getattr(self.state["engine"], "app_name", None) == wanted:
                return
            log.info("Re-opening %r for %s (the shared engine was on %r)",
                     wanted, self.key, open_now)
            self._attach(wanted)

    def _close_quietly(self, engine_):
        if engine_ is None:
            return
        try:
            engine_.close()
        except Exception:
            log.warning("Could not close the engine cleanly", exc_info=True)

    def _teardown_engine(self):
        engine_ = self.state["engine"]
        self.state["engine"] = None
        self.state["app_name"] = None
        if self.shares_engine and _shared["engine"] is engine_:
            _shared["engine"] = None
            _forget_shared(engine_, except_=self)
        self._close_quietly(engine_)

    def _attach(self, app_name):
        if self.impersonates:
            if not self.connected():
                self.state["engine"] = self._new_engine()
            self.state["engine"].open_app(app_name)
            self.state["app_name"] = app_name
            return self.state["engine"]

        with _engine_lock:
            engine_ = self.state["engine"]
            if engine_ is None or not engine_.connected:
                engine_ = _shared["engine"]

            on_now = getattr(engine_, "app_name", None) if engine_ else None
            if engine_ is not None and engine_.connected and on_now not in (None, app_name):
                _forget_shared(engine_, except_=self)
                _shared["engine"] = None
                self._close_quietly(engine_)
                engine_ = None

            if engine_ is None or not engine_.connected:
                engine_ = self._new_engine()

            engine_.open_app(app_name)
            _shared["engine"] = engine_
            self.state["engine"] = engine_
            self.state["app_name"] = app_name
            return engine_

    def open_app(self, app_name=None):
        app_name = app_name or APP_NAME

        with self.lock:
            self.touched = time.monotonic()
            if self.connected() and self.state["app_name"] == app_name:
                return self.state["engine"]

            previous = self.state["app_name"] if self.connected() else None
            if previous is not None:
                self.reset_chat()
                self._teardown_engine()

            try:
                self._attach(app_name)
            except Exception:
                self._teardown_engine()
                if previous is not None and previous != app_name:
                    self._reopen(previous)
                raise

            if previous is None:
                self.reset_chat()
            return self.state["engine"]

    def _reopen(self, app_name):
        try:
            self._attach(app_name)
        except Exception:
            log.warning("Could not reopen %r after a failed switch", app_name,
                        exc_info=True)
            self._teardown_engine()

    def ensure_open(self):
        if not self.connected():
            self.open_app(APP_NAME)
        return self.state["engine"]

    def app_name(self):
        return self.state["app_name"]

    def list_apps(self):
        with self.lock:
            if self.connected():
                return [a["name"] for a in self.state["engine"].list_apps() if a["name"]]

            scratch = self._new_engine()
            try:
                return [a["name"] for a in scratch.list_apps() if a["name"]]
            finally:
                try:
                    scratch.close()
                except Exception:
                    log.debug("Could not close the scratch connection", exc_info=True)

    def close(self):
        with self.lock:
            if self.state["engine"] is not None:
                try:
                    self.state["engine"].close()
                except Exception:
                    log.warning("Could not close %s cleanly", self.key, exc_info=True)
            self.state["engine"] = None
            self.state["app_name"] = None

    def allow_reload(self):
        return self.state["allow_reload"]

    def set_allow_reload(self, value):
        with self.lock:
            self.state["allow_reload"] = bool(value)
        return self.state["allow_reload"]

    def language(self):
        return self.state["language"]

    def set_language(self, code):
        with self.lock:
            self.state["language"] = (code or "").strip().lower()
            messages_ = self.state["messages"]
            if messages_ and messages_[0].get("role") == "system":
                messages_[0]["content"] = system_prompt(self.state["language"])

    def client(self):
        with self._own_lock:
            if self.state["client"] is None:
                self.state["client"] = llm.build_client()
            return self.state["client"]

    def model(self):
        return self.state["model"]

    def set_model(self, name):
        from llm import supports_tools

        if not supports_tools(self.client(), name):
            raise ModelError(f"{name} cannot call tools, so it can't run the assistant.")
        with self._own_lock:
            self.state["model"] = name
        return name

    def resolve_model(self, preferred=None):
        chosen, note = pick_tool_model(
            self.client(),
            preferred or self.state["model"] or llm.default_model())
        if not chosen:
            raise ModelError("No usable model.")
        with self._own_lock:
            self.state["model"] = chosen
        return chosen, note

    def assistant_ready(self):
        return bool(self.state["model"])

    def ensure_model(self):
        if self.state["model"]:
            return self.state["model"]

        with self._own_lock:
            self.state["client"] = None
        return self.resolve_model(llm.default_model())[0]

    def messages(self):
        return self.state["messages"]

    def chat_id(self):
        return self.state["chat_id"]

    def persist(self):
        with self._own_lock:
            if self.state["chat_id"] is None:
                self.state["chat_id"] = history.new_id()
            try:
                return history.save(self.state["chat_id"], self.state["messages"],
                                    self.state["app_name"], owner=self.owner)
            except Exception:
                log.exception("Could not save chat history")
                return None

    def reset_chat(self):
        with self._own_lock:
            if self.state["chat_id"] is not None:
                self.persist()
            self.state["chat_id"] = history.new_id()
            self.state["messages"] = [
                {"role": "system", "content": system_prompt(self.state["language"])}
            ]
        return self.state["messages"]

    def load_chat(self, chat_id_):
        record = history.load(chat_id_, self.owner)
        if record is None:
            return None

        with self.lock:
            if self.state["chat_id"] is not None and self.state["chat_id"] != chat_id_:
                self.persist()
            wanted = record.get("app")
            if wanted and self.state["app_name"] and wanted != self.state["app_name"]:
                try:
                    self.open_app(wanted)
                except Exception as e:
                    log.warning("Could not reopen app %r for chat %s: %s",
                                wanted, chat_id_, e)
                    record["app_mismatch"] = (
                        f"This chat is about the app {wanted!r}, which could not "
                        f"be reopened: {e}"
                    )
            messages_ = record["messages"]
            if not messages_ or messages_[0].get("role") != "system":
                messages_ = [
                    {"role": "system", "content": system_prompt(self.state["language"])}
                ] + messages_
            self.state["messages"] = messages_
            self.state["chat_id"] = chat_id_
        return record

    def delete_chat(self, chat_id_):
        with self._own_lock:
            history.delete(chat_id_, self.owner)
            if self.state["chat_id"] == chat_id_:
                self.state["chat_id"] = history.new_id()
                self.state["messages"] = [
                    {"role": "system", "content": system_prompt(self.state["language"])}
                ]
        return True

    def listing(self):
        return history.listing(self.owner)


def _forget_shared(engine_, except_=None):
    if engine_ is None:
        return
    for sess in list(_sessions.values()):
        if sess is except_ or sess.state["engine"] is not engine_:
            continue
        sess.state["engine"] = None
        sess._displaced = True


def system():
    with _registry_lock:
        if SYSTEM_KEY not in _sessions:
            _sessions[SYSTEM_KEY] = Session(SYSTEM_KEY)
        return _sessions[SYSTEM_KEY]


def key_for(user):
    if isinstance(user, dict):
        user = user.get("id")
    return f"u{int(user)}"


def for_user(user):
    if not isinstance(user, dict):
        raise TypeError("for_user needs the user record, not just an id.")

    key = key_for(user)
    going = []
    with _registry_lock:
        existing = _sessions.get(key)
        if existing is None:
            going = _stale()
            fresh = Session(key, user)
            fresh.state["model"] = system().state["model"]
            _sessions[key] = fresh

    if existing is None:
        _reap(going)
        return fresh

    directory = (user.get("qlik_directory") or "").strip()
    user_id = (user.get("qlik_user_id") or "").strip()
    if (directory, user_id) != (existing.qlik_directory, existing.qlik_user_id):
        log.info("Qlik identity for %s changed; reconnecting", existing.key)
        existing.close()
        existing.qlik_directory = directory
        existing.qlik_user_id = user_id
    existing.username = user.get("username") or existing.username
    existing.touched = time.monotonic()
    return existing


def _stale():
    if len(_sessions) <= MAX_USER_SESSIONS:
        return []
    cutoff = time.monotonic() - USER_SESSION_IDLE_MINUTES * 60
    going = []
    for key, sess in sorted(_sessions.items(), key=lambda kv: kv[1].touched):
        if len(_sessions) - len(going) <= MAX_USER_SESSIONS:
            break
        if key == SYSTEM_KEY or sess.touched > cutoff:
            continue
        going.append(sess)
    for sess in going:
        _sessions.pop(sess.key, None)
    return going


def _reap(going):
    for sess in going:
        log.info("Closing idle session %s", sess.key)
        try:
            sess.persist()
            sess.close()
        except Exception:
            log.warning("Could not close idle session %s", sess.key, exc_info=True)


def sessions():
    with _registry_lock:
        return list(_sessions.values())


def drop(user):
    key = key_for(user)
    with _registry_lock:
        sess = _sessions.pop(key, None)
    if sess is not None:
        try:
            sess.persist()
        finally:
            sess.close()
    return sess is not None


def reset_for_tests():
    with _registry_lock:
        for key in [k for k in _sessions if k != SYSTEM_KEY]:
            _sessions.pop(key, None)
    _shared["engine"] = None


def close_all():
    for sess in sessions():
        try:
            sess.persist()
        except Exception:
            log.warning("Could not save %s on shutdown", sess.key, exc_info=True)
        try:
            sess.close()
        except Exception:
            log.warning("Could not close %s on shutdown", sess.key, exc_info=True)


def __getattr__(name):
    if name == "_state":
        return system().state
    if name == "_lock":
        return system().lock
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def lock():
    return system().lock


def connected():
    return system().connected()


def engine():
    return system().engine()


def list_apps():
    return system().list_apps()


def open_app(app_name=None):
    return system().open_app(app_name)


def ensure_open():
    return system().ensure_open()


def app_name():
    return system().app_name()


def close():
    return system().close()


def allow_reload():
    return system().allow_reload()


def set_allow_reload(value):
    return system().set_allow_reload(value)


def language():
    return system().language()


def set_language(code):
    return system().set_language(code)


def client():
    return system().client()


def model():
    return system().model()


def set_model(name):
    return system().set_model(name)


def resolve_model(preferred=None):
    return system().resolve_model(preferred)


def assistant_ready():
    return system().assistant_ready()


def ensure_model():
    return system().ensure_model()


def messages():
    return system().messages()


def chat_id():
    return system().chat_id()


def persist():
    return system().persist()


def reset_chat():
    return system().reset_chat()


def load_chat(chat_id_):
    return system().load_chat(chat_id_)


def delete_chat(chat_id_):
    return system().delete_chat(chat_id_)
