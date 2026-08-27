"""One Qlik session and conversation per person.

This module used to hold a single module-level `_state`: one engine, one
message list, one chat id, shared by the web UI, the assistant and the MCP
tools. That was right for what this was - a single-operator tool where the
browser and an MCP client deliberately work on the same open app, because
Qlik Sense Desktop allows only one session per app and two connections would
lock each other out.

It does not survive a second person. Two users of the shared version were in
the *same* conversation: each saw the other's questions, and the second
person's request landed in the middle of the first person's context.

So the state is now a `Session`, and there is one per logged-in user:

    session.for_user(user)      the person's own engine, chat and settings
    session.system()            the CLI, the stdio MCP server, one-off scripts

The module-level functions below are the system session, unchanged, so
`chat.py`, `mcp_server.py` and `check_connection.py` work exactly as before.
The web app resolves a Session per request and passes it explicitly rather
than reading an ambient one - a request that picked up the wrong session
would be one user acting inside another's Qlik identity, which is the single
worst thing this code could do.

**How the engine is shared depends on the mode, because Qlik does.**

- *Enterprise*: each user gets their own websocket, opened with their own
  `X-Qlik-User` header. The engine then applies that user's own app
  permissions - a user sees the apps the QMC grants them and no others, and
  anything they build is owned by them. This is the mode that matters in
  production.

- *Desktop*: there is exactly one engine, shared by everyone, because
  Desktop permits one session per app and has no identities to impersonate.
  Logins, conversations, history and the admin pages are all still per user;
  only the Qlik connection underneath is common. Sessions can disagree about
  which app should be open, so the document is re-opened for whoever is
  asking - serialised on a process-wide lock. That is slow if two people
  ping-pong between apps, and it is the honest behaviour: the alternative is
  answering one person's question against another person's data.
"""

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
from ollama_client import OllamaError, pick_tool_model
from qlik_engine import QlikEngine, QlikEngineError, QlikNotConnectedError

log = logging.getLogger(__name__)

SYSTEM_KEY = "system"


class QlikConnectionLost(QlikNotConnectedError):
    """The connection went away and could not be re-opened just now.

    Its own type because the web layer answers it differently from every
    other engine error: this is the one the Reconnect button is for, and
    the one that is worth retrying by itself in a moment.
    """

# Held while the shared desktop engine is used or switched between apps.
# Enterprise sessions never take it - they own their sockets.
_engine_lock = threading.RLock()

# The one connection every non-impersonating session works through. Qlik
# Sense Desktop permits one session per app, so a second socket alongside a
# live one does not give a second user their own view - it fails to open the
# app at all. There is therefore exactly one, and sessions take turns.
_shared = {"engine": None}

# The session registry. RLock because creating a session can reap others.
_registry_lock = threading.RLock()
_sessions = {}


def per_user_engines():
    """Whether each user gets a Qlik connection of their own.

    Enterprise only. Desktop has no way to be two users at once, and the
    engine there permits one session per app regardless.
    """
    return QLIK_MODE == ENTERPRISE


# ----------------------------------------------------------------------


class Session:
    """One person's engine, conversation and settings."""

    def __init__(self, key, user=None):
        self.key = key
        # A plain dict rather than the user row, so a Session outlives an
        # edit to the account without holding a stale copy of it.
        user = user or {}
        self.user_id = user.get("id")
        self.username = user.get("username") or key
        self.qlik_directory = (user.get("qlik_directory") or "").strip()
        self.qlik_user_id = (user.get("qlik_user_id") or "").strip()

        # Where this person's conversations are filed. None for the system
        # session, which writes into the history root as it always has.
        self.owner = history.owner_key(self.user_id) if self.user_id else None

        self.touched = time.monotonic()

        # Set when somebody else's app switch took the shared connection out
        # from under this session. It is the difference between "nothing is
        # open" - which the person has to act on - and "your socket was
        # replaced", which they did not cause and should never have to know
        # about. See engine().
        self._displaced = False

        # RLock: opening an app resets the conversation, so these nest.
        self._own_lock = threading.RLock()

        # When this session last tried to get its connection back, and what
        # went wrong. Qlik being down is the case that needs both: without
        # the timestamp every request from every user retries, so a server
        # that is already struggling gets a connection attempt per request
        # per user; without the message the requests inside the cooldown
        # have nothing to tell the person except silence.
        self._last_attempt = 0.0
        self._last_failure = None

        self.state = {
            "engine": None,
            "app_name": None,
            # None until a model has been resolved *and* confirmed to support
            # tools. Seeding this with the configured name made
            # assistant_ready() true while Ollama was down, so requests
            # sailed past the check and failed later as a raw connection
            # error.
            "model": None,
            "client": None,
            "messages": [],
            # The saved conversation these messages belong to. Minted on the
            # first reset or the first save, so a session that is never
            # chatted to writes nothing to disk.
            "chat_id": None,
            # Whether the assistant may run a reload itself. On by default:
            # with it off, "load my data and chart it" stalls halfway and the
            # model reports that reloading is impossible rather than that it
            # needs a click.
            "allow_reload": True,
            # Which language the assistant answers in. "" lets it follow
            # whatever the question was written in, which is the right
            # default until someone says otherwise.
            "language": "",
        }

    def __repr__(self):
        return f"<Session {self.key} app={self.state['app_name']!r}>"

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    @property
    def impersonates(self):
        """Whether this session connects to Qlik as its own user.

        False for a user with no Qlik identity recorded, and for every user
        on Desktop. Those fall back to the identity in .env, which is what
        makes the system demonstrable before certificates exist.
        """
        return bool(per_user_engines() and self.qlik_directory and self.qlik_user_id)

    @property
    def shares_engine(self):
        return not self.impersonates

    @property
    def lock(self):
        """What to hold for a run of engine calls that must not interleave.

        A shared engine needs a shared lock, or one user's app switch lands
        in the middle of another user's turn. A session with its own socket
        only needs to keep its own turns apart, which is what lets two
        enterprise users actually work at the same time.
        """
        return _engine_lock if self.shares_engine else self._own_lock

    def _new_engine(self):
        """A connection for this session, as this session's user."""
        if self.impersonates:
            return QlikEngine(user_directory=self.qlik_directory,
                              user_id=self.qlik_user_id)
        # No arguments: the defaults come from config, and the tests replace
        # this callable with a zero-argument fake.
        return QlikEngine()

    # ------------------------------------------------------------------
    # Qlik
    # ------------------------------------------------------------------

    def connected(self):
        return self.state["engine"] is not None and self.state["engine"].connected

    def engine(self):
        """The open engine - reconnecting first if the connection went away.

        A session that has an app on record and no live socket has had one
        taken from it, and the person did not do it: either somebody else's
        app switch replaced the shared Desktop connection, or the network
        dropped - a restarted engine, a firewall idle-timeout, a lost VPN.

        Both used to end here, with "No app is open. Open one first.", and
        both used to stay that way. Nothing retried, so that message repeated
        on every request until an administrator restarted the server: one
        dropped packet cost everybody the product for as long as it took
        somebody to notice. Now the socket is rebuilt and the app the person
        was working on is re-opened, and in the ordinary case - Qlik is fine,
        this one connection went stale - they never learn anything happened.

        Only a session that has genuinely opened nothing is told so.
        """
        if not self.connected():
            if not self.state["app_name"]:
                raise QlikNotConnectedError("No app is open. Open one first.")
            self._recover()
        if self.shares_engine:
            self._ensure_document()
        return self.state["engine"]

    def _recover(self):
        """Rebuild the connection, at a rate a struggling server can take.

        The cooldown is the point of this. When Qlik is genuinely down every
        request from every user arrives here, and each attempt costs the
        connect timeout - so without it a hundred users produce a hundred
        connection attempts per round, each waiting ten seconds, each holding
        a worker thread. That is how one unreachable server makes this one
        unreachable too.

        Inside the cooldown the last real reason is repeated instead, which
        is a better answer than a fresh timeout anyway: it says what is wrong
        now rather than making the person wait ten seconds to be told the
        same thing.
        """
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
        """Try again now, whatever the cooldown says. The Reconnect button.

        Somebody pressing a button is new information: they can see that Qlik
        is back and this code cannot. The cooldown exists to stop automatic
        retries from flooding a server, so it does not apply to a person
        deciding to retry - there is one of them, and they are watching.
        """
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
        """Why the connection could not be rebuilt, or None if it is fine.

        Cleared as soon as a reconnect succeeds, so this is the current
        reason rather than the last one that ever happened.
        """
        return self._last_failure

    def health(self):
        """What this session can and cannot reach, for the page and the admin.

        Deliberately cheap and non-throwing: it is called precisely to find
        out whether something is broken, so it must not break in the process.
        """
        try:
            ready = self.assistant_ready()
        except Exception:  # a health check that fails is worse than useless
            ready = False
        return {
            "app": self.state["app_name"],
            "qlik_connected": self.connected(),
            "qlik_error": self._last_failure,
            "assistant_ready": ready,
            "model": self.state["model"],
        }

    def _ensure_document(self):
        """Point the shared engine at the app THIS session is working on.

        Everyone on Desktop works through one engine, so the last person to
        switch app leaves it open on theirs. Without this, the next request
        from anybody else is answered against the wrong data model and says
        nothing about it - the failure is silent and the answer is confident,
        which is the worst combination available.

        Nothing happens when the engine has no app on record. That is a
        connection that has not opened one yet, or a stand-in supplied by a
        test, and neither is a document to arbitrate over.
        """
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
        """Back to "nothing open", without letting a failed close hide why."""
        engine_ = self.state["engine"]
        self.state["engine"] = None
        self.state["app_name"] = None
        if self.shares_engine and _shared["engine"] is engine_:
            _shared["engine"] = None
            _forget_shared(engine_, except_=self)
        self._close_quietly(engine_)

    def _attach(self, app_name):
        """Get this session onto `app_name`. No conversation side effects.

        Kept apart from open_app() below because they are different events:
        this is plumbing that also runs when the shared connection has to be
        pointed back at the app a session was already using, and that must
        not throw away the conversation the person is in the middle of.

        A live socket is reused. Only a socket that is open on a *different*
        document is replaced, and only then because it has to be: the engine
        has no CloseDoc, and a document is released when the session holding
        it disconnects and not before.
        """
        if self.impersonates:
            if not self.connected():
                self.state["engine"] = self._new_engine()
            self.state["engine"].open_app(app_name)
            self.state["app_name"] = app_name
            return self.state["engine"]

        with _engine_lock:
            engine_ = self.state["engine"]
            if engine_ is None or not engine_.connected:
                # Somebody else may already have the shared connection open.
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
        """Open an app. Changing app starts a new conversation, because it must.

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

        with self.lock:
            self.touched = time.monotonic()
            if self.connected() and self.state["app_name"] == app_name:
                return self.state["engine"]

            previous = self.state["app_name"] if self.connected() else None
            if previous is not None:
                # Filed while the app it was asked against is still the open
                # one, so the chat is not recorded against the app replacing
                # it.
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
                # Nothing was filed above, so this is the first app of the
                # session: whatever was said before it was open was not
                # about it.
                self.reset_chat()
            return self.state["engine"]

    def _reopen(self, app_name):
        """Go back to the app that was open before a switch failed.

        The old session had to be dropped to try the new app at all, so
        failing without this would leave nothing open at all - a worse place
        than the person started from, for asking for an app they could not
        have.
        """
        try:
            self._attach(app_name)
        except Exception:
            log.warning("Could not reopen %r after a failed switch", app_name,
                        exc_info=True)
            self._teardown_engine()

    def ensure_open(self):
        """Open the configured app if nothing is open yet."""
        if not self.connected():
            self.open_app(APP_NAME)
        return self.state["engine"]

    def app_name(self):
        return self.state["app_name"]

    def list_apps(self):
        """Every app this user can see, whether one is open or not.

        Listing needs a connection, not an open document, but the picker used
        to ask through engine() - which refuses when nothing is open - and
        fell back to showing the single app it already had. With one entry in
        the list there was nothing to switch TO, so the app could not be
        changed at all from a session that had lost its document.

        On enterprise the scratch connection carries this user's identity, so
        the list is genuinely theirs rather than the service account's.
        """
        with self.lock:
            if self.connected():
                return [a["name"] for a in self.state["engine"].list_apps() if a["name"]]

            # Only when there is no connection to borrow: Qlik Sense Desktop
            # allows one session per app, so a second socket alongside a live
            # one is exactly what this module exists to avoid.
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

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def allow_reload(self):
        return self.state["allow_reload"]

    def set_allow_reload(self, value):
        with self.lock:
            self.state["allow_reload"] = bool(value)
        return self.state["allow_reload"]

    def language(self):
        return self.state["language"]

    def set_language(self, code):
        """Pin the assistant's language, including for the open conversation.

        The system prompt is written once when a conversation starts, so
        without rewriting it here the setting would not take effect until the
        next chat - which reads as the switch being broken.
        """
        with self.lock:
            self.state["language"] = (code or "").strip().lower()
            messages_ = self.state["messages"]
            if messages_ and messages_[0].get("role") == "system":
                messages_[0]["content"] = system_prompt(self.state["language"])

    # ------------------------------------------------------------------
    # Model
    # ------------------------------------------------------------------

    def client(self):
        # The lock, like every other state write: two first requests arriving
        # together would otherwise each install their own client.
        with self._own_lock:
            if self.state["client"] is None:
                # Which provider this is comes from configuration, not from
                # here - the assistant calls the same three methods either
                # way (see llm.py).
                self.state["client"] = llm.build_client()
            return self.state["client"]

    def model(self):
        return self.state["model"]

    def set_model(self, name):
        from ollama_client import supports_tools

        if not supports_tools(self.client(), name):
            raise OllamaError(f"{name} cannot call tools, so it can't run the assistant.")
        with self._own_lock:
            self.state["model"] = name
        return name

    def resolve_model(self, preferred=None):
        """Settle on a model that can drive the assistant, and remember it."""
        chosen, note = pick_tool_model(
            self.client(),
            preferred or self.state["model"] or llm.default_model())
        if not chosen:
            raise OllamaError("No usable model.")
        with self._own_lock:
            self.state["model"] = chosen
        return chosen, note

    def assistant_ready(self):
        """Whether there is a usable model. False when Ollama isn't running."""
        return bool(self.state["model"])

    def ensure_model(self):
        """Resolve a model now if we don't have one yet.

        Called on each assistant request so Ollama can be started *after*
        this server, and simply begin working - rather than the whole product
        refusing to open because one of its two dependencies was down at
        startup.
        """
        if self.state["model"]:
            return self.state["model"]

        # Fresh client: the previous one cached a dead connection.
        with self._own_lock:
            self.state["client"] = None
        return self.resolve_model(llm.default_model())[0]

    # ------------------------------------------------------------------
    # Conversation
    # ------------------------------------------------------------------

    def messages(self):
        return self.state["messages"]

    def chat_id(self):
        return self.state["chat_id"]

    def persist(self):
        """Write the open conversation to disk. Never fatal to a chat turn.

        An unwritable history directory should cost the user their history,
        not the answer they are waiting for - so a failure here is logged and
        the turn still returns.
        """
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
        """Start a new conversation, keeping the one being replaced.

        Also reached by open_app(): switching apps ends the chat, and the old
        one is worth keeping even though nothing asked for it to be saved.
        """
        with self._own_lock:
            if self.state["chat_id"] is not None:
                self.persist()
            self.state["chat_id"] = history.new_id()
            self.state["messages"] = [
                {"role": "system", "content": system_prompt(self.state["language"])}
            ]
        return self.state["messages"]

    def load_chat(self, chat_id_):
        """Reopen a saved conversation as the live one.

        The saved messages become the model's context again, not just the
        transcript on screen - otherwise the assistant would remember nothing
        of the chat the user is looking at.
        """
        record = history.load(chat_id_, self.owner)
        if record is None:
            return None

        with self.lock:
            if self.state["chat_id"] is not None and self.state["chat_id"] != chat_id_:
                self.persist()
            # The saved answers are about the app they were asked against.
            # Restoring them on top of a different open app leaves the model
            # reasoning about the wrong data model, so switch back first -
            # the reset that open_app does is overwritten just below.
            wanted = record.get("app")
            if wanted and self.state["app_name"] and wanted != self.state["app_name"]:
                try:
                    self.open_app(wanted)
                except Exception as e:
                    # Still load the chat - losing the transcript would be
                    # worse - but tell the caller instead of silently
                    # proceeding.
                    log.warning("Could not reopen app %r for chat %s: %s",
                                wanted, chat_id_, e)
                    record["app_mismatch"] = (
                        f"This chat is about the app {wanted!r}, which could not "
                        f"be reopened: {e}"
                    )
            messages_ = record["messages"]
            # A history file written before the prompt changed - or
            # hand-edited - still has to start with the rules the tools are
            # described by.
            if not messages_ or messages_[0].get("role") != "system":
                messages_ = [
                    {"role": "system", "content": system_prompt(self.state["language"])}
                ] + messages_
            self.state["messages"] = messages_
            self.state["chat_id"] = chat_id_
        return record

    def delete_chat(self, chat_id_):
        """Remove a saved chat, starting a new one if it was the open chat."""
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


# ----------------------------------------------------------------------
# The registry
# ----------------------------------------------------------------------

def _forget_shared(engine_, except_=None):
    """Unhook every session from a shared connection that is going away.

    They keep the app they were working on, and are marked as displaced so
    that their next request re-opens it. Leaving them pointing at a closed
    socket would surface as "no app is open" to somebody who never closed
    anything - the person who switched app was somebody else.
    """
    if engine_ is None:
        return
    for sess in list(_sessions.values()):
        if sess is except_ or sess.state["engine"] is not engine_:
            continue
        sess.state["engine"] = None
        sess._displaced = True


def system():
    """The session with no user behind it: the CLI, stdio MCP, scripts."""
    with _registry_lock:
        if SYSTEM_KEY not in _sessions:
            _sessions[SYSTEM_KEY] = Session(SYSTEM_KEY)
        return _sessions[SYSTEM_KEY]


def key_for(user):
    """The registry key for a user row, or for a bare id."""
    if isinstance(user, dict):
        user = user.get("id")
    return f"u{int(user)}"


def for_user(user):
    """This user's session, created on first use.

    The user row is re-read into the session each time, so an administrator
    changing somebody's Qlik identity takes effect on their next request
    rather than at their next login. A changed identity drops the connection
    it was opened with - it is the wrong person's socket now.
    """
    if not isinstance(user, dict):
        raise TypeError("for_user needs the user record, not just an id.")

    key = key_for(user)
    going = []
    with _registry_lock:
        existing = _sessions.get(key)
        if existing is None:
            going = _stale()
            fresh = Session(key, user)
            # Start from the model the server resolved and checked at
            # startup. Which model to use is the person's own setting and
            # they can change it, but a session that begins with none
            # reports the assistant as unavailable until they have asked it
            # something - so everybody's first look at the page said Ollama
            # was down while it was running perfectly well.
            fresh.state["model"] = system().state["model"]
            _sessions[key] = fresh

    if existing is None:
        _reap(going)          # outside the lock on purpose - see _reap
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
    """Choose the sessions to let go of, and unhook them from the registry.

    Runs with `_registry_lock` held. It only chooses and removes; closing
    them is deliberately somebody else's job - see _reap.
    """
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
    """Close sessions nobody has used for a while.

    Each one holds a Qlik socket in enterprise mode, and an engine session
    nobody is using is still an open document and a licence seat on the
    server. Called when a new session is created rather than on a timer, so
    there is no background thread to shut down.

    This must run OUTSIDE `_registry_lock`, which is the reason it is split
    from _stale(). Persisting writes to disk, and closing writes to a
    websocket and then waits for the reply - and a socket whose peer has gone
    away waits the full timeout for a reply that is never coming. Every
    request calls for_user() and so takes that lock, so doing this inside it
    stalled all of them behind one reap: measured at 0.95 seconds for
    requests that needed nothing but a dictionary lookup, and that was
    against a fake socket. The real wait is longest during an outage, which
    is exactly when the sessions being reaped are the ones that cannot close
    cleanly.
    """
    for sess in going:
        log.info("Closing idle session %s", sess.key)
        try:
            sess.persist()
            sess.close()
        except Exception:
            log.warning("Could not close idle session %s", sess.key, exc_info=True)


def sessions():
    """Every live session, for the admin page and for shutdown."""
    with _registry_lock:
        return list(_sessions.values())


def drop(user):
    """Forget a user's session entirely - they were disabled or deleted."""
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
    """Forget every per-user session and the shared connection.

    The system session is deliberately left in place: it is the state the
    older tests set up and tear down themselves, and replacing it here would
    pull the ground out from under them.
    """
    with _registry_lock:
        for key in [k for k in _sessions if k != SYSTEM_KEY]:
            _sessions.pop(key, None)
    _shared["engine"] = None


def close_all():
    """Shut every session down, saving whatever was being said."""
    for sess in sessions():
        try:
            sess.persist()
        except Exception:
            log.warning("Could not save %s on shutdown", sess.key, exc_info=True)
        try:
            sess.close()
        except Exception:
            log.warning("Could not close %s on shutdown", sess.key, exc_info=True)


# ----------------------------------------------------------------------
# The system session, as plain module functions
#
# Everything below is the old module-level API, unchanged, operating on the
# system session. chat.py, mcp_server.py and check_connection.py are
# single-user by nature and go on calling these; the web app resolves a
# Session per request instead.
# ----------------------------------------------------------------------

def __getattr__(name):
    """`session._state` still means "the state", now the system session's.

    Python only calls this when normal attribute lookup fails, so there must
    be no module-level `_state` for it to find. Kept because a great deal of
    code and test setup reads and writes it directly, and because on a
    single-operator install it is still the plain truth: there is one state.
    """
    if name == "_state":
        return system().state
    if name == "_lock":
        return system().lock
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def lock():
    """The shared lock, for callers that need several steps to be atomic."""
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
