"""Qlik + local AI, in one place.

    python web_app.py

Opens one server that is the whole product:

    http://127.0.0.1:8000        the load editor, with the assistant
                                 underneath it
    http://127.0.0.1:8000/admin  accounts, other people's chats, the audit
                                 trail - administrators only
    http://127.0.0.1:8000/mcp    the same session over MCP, for Claude
                                 Code / Claude Desktop

Everyone signs in, and everything below the login is per person: their own
conversation, their own history, and - on Qlik Sense Enterprise - their own
Qlik connection opened as *them*, so the QMC's app permissions apply to what
they can see and anything they build belongs to them (see session.py).

On Qlik Sense Desktop there is one engine and everyone shares it, because
Desktop permits one session per app and has no identities to impersonate.
The logins, conversations and admin pages are real there too; only the Qlik
connection underneath is common.

Nothing leaves the machine: browser -> this server -> Qlik and Ollama.
"""

import argparse
import contextlib
import ipaddress
import json
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import anyio.to_thread
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel

import auth
import directory
import history
import llm
import session
import users
from auth import Identity, qlik_session, require_admin, require_user
from chat_tools import LOAD_EDITOR_TOOLS, run_agent, stream_agent
from config import (
    APP_NAME,
    AUTH_ENABLED,
    CHAT_MODEL,
    QLIK_MODE,
    REQUIRE_QLIK_AT_STARTUP,
    LOG_BACKUPS,
    LOG_FILE,
    LOG_LEVEL,
    LOG_MAX_MB,
    WORKER_THREADS,
)
from data_prep import tab_names
from mcp_server import mcp
from ollama_client import OllamaError, describe_models
from qlik_engine import QlikEngineError

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
INDEX = HERE / "web" / "index.html"
LOGIN = HERE / "web" / "login.html"
ADMIN = HERE / "web" / "admin.html"

# Everything, including reloading. Reloading is gated by a setting rather
# than withheld: a model that is simply refused concludes the action is
# impossible and tells the user it cannot be done, which turns "load my data
# and chart it" into a dead end.
ASSISTANT_TOOLS = LOAD_EDITOR_TOOLS | {
    "build_dashboard", "create_chart", "list_charts", "edit_chart", "check_expression",
    "analyze_sheet", "save", "open_app", "reload_data",
}


def _may_run(sess):
    """Answer a destructive action's confirmation on the browser's behalf.

    There is nowhere to ask mid-request - the answer is already streaming -
    so each action is decided by what the person has already agreed to. The
    one switch here says the assistant may LOAD DATA, and it is not a
    licence to throw away the load script or a sheet full of charts as well:
    those are refused, and the model is told to work in its own tab, or to
    ask for the deletion in so many words.

    delete_sheet was briefly allowed here and taken out again. The engine
    side works - a sheet named to delete_sheet is removed, charts and all -
    but a browser turn has no confirmation step, so "delete all the sheets"
    is 23 irreversible deletions decided by how a small model read one line
    of text. The terminal chat asks before each one and keeps it. Anyone
    re-enabling this should add the prompt, not just the permission.

    The setting is now the asking user's own, which is why this is built per
    request rather than being one module-level function.
    """
    def may(question, action):
        return action == "reload_data" and sess.allow_reload()
    return may


def widen_the_threadpool():
    """Let more than forty people use this at once.

    Every endpoint here is a plain `def`, which Starlette runs in a worker
    thread, and a chat turn holds its thread for the whole turn - tool calls
    and model latency included. anyio's default pool is forty threads, so the
    forty-first concurrent chat did not queue politely: it blocked the whole
    server, login page included, until one of the forty finished.

    That ceiling was invisible because it is nowhere in this codebase - it is
    a default several layers down, and the failure it produces looks like the
    network being down rather than like a limit being reached.

    Safe to raise because the work is I/O-bound: a websocket to Qlik and an
    HTTPS call to the model, both asleep almost all the time.
    """
    limiter = anyio.to_thread.current_default_thread_limiter()
    before = limiter.total_tokens
    if WORKER_THREADS > before:
        limiter.total_tokens = WORKER_THREADS
    return before, limiter.total_tokens


@contextlib.asynccontextmanager
async def lifespan(_app):
    was, now = widen_the_threadpool()
    if now != was:
        log.info("Request threads: %d (anyio default is %d)", now, was)

    # The MCP session manager needs its own lifespan running, or /mcp 500s on
    # first use.
    async with contextlib.AsyncExitStack() as stack:
        await stack.enter_async_context(mcp.session_manager.run())
        yield
    session.close_all()
    llm.close_shared()


app = FastAPI(title="Qlik AI", lifespan=lifespan)


@app.middleware("http")
async def _gate(request: Request, call_next):
    """Nothing but the login page is reachable without signing in.

    Middleware rather than a dependency on each endpoint, because `/mcp` is
    a mounted sub-application and never sees route dependencies - and it is
    the endpoint that can rewrite the load script and reload the data.
    """
    return await auth.gate(request, call_next)


def _engine(sess):
    """The engine for this request, opening the default app on a first visit.

    A session that has never opened anything is somebody who has just signed
    in. Starting the server opens the configured app for the *system*
    session, which is nobody's - so without this every user's first request
    reported that no app was open, which reads as the product being broken
    rather than as a step they had missed.

    Only when `app_name` is None. A session that HAS an app and is not
    connected is a different thing - the engine went away - and that has to
    surface rather than being silently re-opened as something else.
    """
    try:
        if sess.app_name() is None:
            sess.open_app(APP_NAME)
        return sess.engine()
    except session.QlikConnectionLost as e:
        # The one engine failure a person can do something about. The header
        # is what lets the page offer Reconnect instead of printing a red
        # line about a websocket, which nobody outside this file can act on.
        raise HTTPException(503, str(e), headers={"X-Qlik-Reconnect": "1"}) from e
    except QlikEngineError as e:
        raise HTTPException(503, str(e)) from e


def transcript(messages):
    """The part of a conversation worth showing back to the person.

    The stored list is what the model sees: the system prompt, tool calls and
    their results are all in there. Replaying those would show the user a wall
    of JSON they never wrote and never read the first time.
    """
    out = []
    for message in messages or []:
        role = message.get("role")
        content = (message.get("content") or "").strip()
        # An assistant message with no text is a bare tool call.
        if role in ("user", "assistant") and content:
            out.append({"role": role, "content": content})
    return out


# ----------------------------------------------------------------------


class ScriptUpdate(BaseModel):
    content: str


class ChatMessage(BaseModel):
    message: str


class Selection(BaseModel):
    app: str = ""
    model: str = ""
    allow_reload: bool | None = None
    # "" means follow whatever language the question was written in.
    language: str | None = None


class Rename(BaseModel):
    title: str


class Credentials(BaseModel):
    username: str
    password: str


class NewUser(BaseModel):
    username: str
    password: str
    display_name: str = ""
    role: str = users.USER
    qlik_directory: str = ""
    qlik_user_id: str = ""
    active: bool = True


class UserEdit(BaseModel):
    display_name: str | None = None
    role: str | None = None
    qlik_directory: str | None = None
    qlik_user_id: str | None = None
    active: bool | None = None


class NewPassword(BaseModel):
    password: str


class Impersonation(BaseModel):
    user_id: int | None = None


# ----------------------------------------------------------------------
# Signing in
# ----------------------------------------------------------------------

# The gate has already resolved who is asking and left it on the request,
# so these read that rather than going back to the database for it.

@app.get("/login")
def login_page(request: Request):
    """The login form, or straight through if there is already a session."""
    if getattr(request.state, "identity", None) is not None:
        return RedirectResponse("/", status_code=303)
    return FileResponse(LOGIN)


@app.get("/api/session")
def get_session(request: Request):
    """Whether anyone is signed in. Public, so the login page can ask."""
    identity = getattr(request.state, "identity", None)
    if identity is None:
        return {"signed_in": False, "auth_enabled": AUTH_ENABLED}
    return {"signed_in": True, "user": identity.summary()}


@app.post("/api/login")
def post_login(request: Request, credentials: Credentials):
    ip = auth.client_ip(request)
    agent = request.headers.get("user-agent", "")

    user, reason = auth.sign_in(credentials.username, credentials.password, ip=ip)
    if user is None:
        users.audit("login.failed", ip=ip,
                    detail={"username": credentials.username, "reason": reason})

        if reason == "locked":
            raise HTTPException(
                429,
                "Too many failed attempts. This account is locked for a few "
                "minutes - ask an administrator if you need it sooner.",
            )
        if reason == "directory":
            # Not the person's fault, and saying "wrong password" here sends
            # them to reset a password that was never the problem.
            raise HTTPException(
                503,
                "The directory could not be reached, so your Windows password "
                "could not be checked. Try again shortly, or tell whoever "
                "runs this server.",
            )
        # One message for "no such user" and for "wrong password". Telling
        # them apart hands an attacker a list of real usernames.
        raise HTTPException(401, "That username and password do not match.")

    token = users.start_session(user["id"], ip=ip, agent=agent)
    users.audit("login", user=user, ip=ip)

    identity = Identity(user, token=token, ip=ip)
    response = JSONResponse({"ok": True, "user": identity.summary()})
    return auth.set_cookie(response, token)


@app.post("/api/logout")
def post_logout(request: Request):
    identity = getattr(request.state, "identity", None)
    if identity is not None and identity.token:
        users.audit("logout", user=identity.user, ip=identity.ip)
        users.end_session(identity.token)
    return auth.clear_cookie(JSONResponse({"ok": True}))


@app.get("/api/me")
def get_me(identity: Identity = Depends(require_user)):
    return identity.summary()


@app.post("/api/me/password")
def change_own_password(request: Request, body: NewPassword,
                        identity: Identity = Depends(require_user)):
    """Change your own password. Not available while acting as someone else."""
    if identity.impersonating:
        raise HTTPException(403, "Stop acting as another user first.")
    if identity.user.get("id") is None:
        raise HTTPException(400, "There is no account to change.")
    try:
        users.set_password(identity.user["id"], body.password)
    except users.UserError as e:
        raise HTTPException(400, str(e)) from e
    users.audit("password.changed", user=identity.user, ip=identity.ip)
    return {"ok": True}


# ----------------------------------------------------------------------
# The product
# ----------------------------------------------------------------------

@app.get("/")
def index():
    return FileResponse(INDEX)


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/health")
def get_health(sess=Depends(qlik_session), identity: Identity = Depends(require_user)):
    """What is up and what is down, without trying to use any of it.

    Separate from /healthz, which stays a flat 200: that one is the service
    manager's probe, and a probe that fails when Qlik is down would have the
    service restarted for somebody else's outage - which is the failure this
    whole change exists to remove.
    """
    return sess.health()


@app.post("/api/reconnect")
def post_reconnect(sess=Depends(qlik_session), identity: Identity = Depends(require_user)):
    """Rebuild this person's Qlik connection now.

    The refresh button. Reconnecting is per person because the connections
    are: on Enterprise everyone has their own socket, opened as them, so one
    user's dead connection is not evidence about anybody else's.
    """
    try:
        engine_ = sess.reconnect()
    except QlikEngineError as e:
        raise HTTPException(503, str(e), headers={"X-Qlik-Reconnect": "1"}) from e
    users.audit("qlik.reconnected", user=identity.effective, ip=identity.ip,
                detail=identity.audited(app=sess.app_name()))
    return {"ok": True, "app": sess.app_name(), "mode": engine_.mode,
            **sess.health()}


@app.post("/api/admin/reconnect-all")
def post_reconnect_all(identity: Identity = Depends(require_admin)):
    """Rebuild every session's connection, so nobody restarts the server.

    This is the button that replaces "the admin restarts the program from the
    server". Qlik came back; this gets everyone onto it without ending the
    logins, throwing away the conversations, or waiting for each person to
    notice and press their own.

    One session's failure is reported, not raised: an account whose Qlik
    identity is wrong must not stop the other ninety-nine being repaired.
    """
    healed, failed = [], []
    for sess in session.sessions():
        if sess.app_name() is None:
            continue
        try:
            sess.reconnect()
            healed.append(sess.key)
        except Exception as e:
            failed.append({"session": sess.key, "error": str(e)})
            log.warning("Could not reconnect %s: %s", sess.key, e)
    users.audit("qlik.reconnected.all", user=identity.user, ip=identity.ip,
                detail=identity.audited(reconnected=len(healed),
                                        failed=len(failed)))
    return {"reconnected": healed, "failed": failed}


@app.get("/api/admin/health")
def get_admin_health(identity: Identity = Depends(require_admin)):
    """Every session's connection at once, for the person on call."""
    return {
        "mode": QLIK_MODE,
        "per_user_engines": session.per_user_engines(),
        "sessions": [
            {"session": sess.key, "user": sess.username, **sess.health()}
            for sess in session.sessions()
        ],
    }


@app.get("/api/state")
def get_state(sess=Depends(qlik_session), identity: Identity = Depends(require_user)):
    """Everything the page needs: script, tabs, connections, sheets."""
    engine_ = _engine(sess)
    script = engine_.get_script()

    def safely(call, default):
        try:
            return call()
        except QlikEngineError as e:
            log.debug("%s unavailable: %s", call, e)
            return default

    return {
        "app": sess.app_name(),
        "model": sess.model(),
        "mode": engine_.mode,
        "script": script,
        "tabs": tab_names(script),
        "connections": safely(engine_.list_connections, []),
        "sheets": safely(engine_.list_sheets, []),
        "has_data": bool(safely(engine_.get_fields, [])),
        "allow_reload": sess.allow_reload(),
        "language": sess.language(),
        "assistant_ready": sess.assistant_ready(),
        "chat_id": sess.chat_id(),
        "transcript": transcript(sess.messages()),
        "user": identity.summary(),
    }


@app.get("/api/options")
def get_options(sess=Depends(qlik_session)):
    """Apps and models the person can switch between."""
    try:
        apps = sess.list_apps()
    except QlikEngineError as e:
        log.debug("Could not list apps: %s", e)
        apps = []

    # The open app belongs in the picker even when the listing failed, or
    # there is nothing to switch back to - but it is no longer the ONLY
    # entry, which is what made the app impossible to change.
    open_now = sess.app_name()
    if open_now and open_now not in apps:
        apps.insert(0, open_now)

    try:
        models = describe_models(sess.client())
    except OllamaError as e:
        log.debug("Could not list models: %s", e)
        # Ollama is down. Say so in the dropdown rather than showing an
        # empty list that looks like a bug in this app.
        models = [{
            "name": sess.model() or "Ollama not running",
            "usable": sess.assistant_ready(),
            "reason": "" if sess.assistant_ready() else "start Ollama, then reload this page",
            "size_gb": 0,
        }]

    return {
        "apps": apps, "app": sess.app_name(),
        "models": models, "model": sess.model(),
    }


@app.post("/api/select")
def post_select(selection: Selection, sess=Depends(qlik_session),
                identity: Identity = Depends(require_user)):
    """Switch app, model or the reload setting without restarting."""
    if selection.allow_reload is not None:
        sess.set_allow_reload(selection.allow_reload)

    if selection.language is not None:
        sess.set_language(selection.language)

    if selection.model and selection.model != sess.model():
        try:
            sess.set_model(selection.model)
        except OllamaError as e:
            raise HTTPException(400, str(e)) from e

    if selection.app and selection.app != sess.app_name():
        try:
            sess.open_app(selection.app)
        except QlikEngineError as e:
            raise HTTPException(400, str(e)) from e
        users.audit("app.opened", user=identity.effective, ip=identity.ip,
                    detail=identity.audited(app=selection.app))

    return {
        "ok": True, "app": sess.app_name(), "model": sess.model(),
        "allow_reload": sess.allow_reload(), "language": sess.language(),
    }


# ----------------------------------------------------------------------
# Data load editor
# ----------------------------------------------------------------------

@app.put("/api/script")
def put_script(update: ScriptUpdate, sess=Depends(qlik_session),
               identity: Identity = Depends(require_user)):
    """Save the script as edited in the browser.

    Syntax-checked by the engine; one that doesn't parse is rejected and the
    previous script stays, so the editor can't leave the app unable to run.
    """
    try:
        _engine(sess).set_script(update.content)
    except QlikEngineError as e:
        raise HTTPException(400, str(e)) from e
    # The script decides what every figure in the app is made of, so who
    # rewrote it is exactly the kind of thing an audit trail is for.
    users.audit("script.saved", user=identity.effective, ip=identity.ip,
                detail=identity.audited(app=sess.app_name(),
                                        characters=len(update.content)))
    return {"ok": True, "tabs": tab_names(update.content)}


@app.post("/api/reload")
def post_reload(sess=Depends(qlik_session), identity: Identity = Depends(require_user)):
    """Run the load script, from the Load data button."""
    engine_ = _engine(sess)
    result = engine_.reload_data()
    if result.get("success"):
        result["tables"] = [
            {"name": t["name"], "rows": t["rows"]} for t in engine_.get_tables()
        ]
        result["total_rows"] = sum(t["rows"] for t in result["tables"])
    users.audit("data.reloaded", user=identity.effective, ip=identity.ip,
                detail=identity.audited(app=sess.app_name(),
                                        success=bool(result.get("success"))))
    return result


@app.post("/api/save")
def post_save(sess=Depends(qlik_session), identity: Identity = Depends(require_user)):
    engine_ = _engine(sess)
    engine_.save()
    users.audit("app.saved", user=identity.effective, ip=identity.ip,
                detail=identity.audited(app=sess.app_name()))
    # The file's timestamp is proof the change reached disk, which is what
    # tells "it didn't save" apart from "Qlik is showing you a cached copy".
    return {"ok": True, "file": engine_.app_file_info()}


# ----------------------------------------------------------------------
# Assistant
# ----------------------------------------------------------------------

@app.post("/api/chat")
def post_chat(body: ChatMessage, sess=Depends(qlik_session),
              identity: Identity = Depends(require_user)):
    """One turn with the assistant, then report what changed."""
    engine_ = _engine(sess)
    actions = []

    try:
        model = sess.ensure_model()
    except OllamaError as e:
        raise HTTPException(503, str(e)) from e

    with sess.lock:
        before = engine_.get_script()
        sheets_before = len(engine_.list_sheets())
        messages = sess.messages()
        messages.append({"role": "user", "content": body.message})

        try:
            reply = run_agent(
                sess.client(), model, engine_, messages,
                allowed=ASSISTANT_TOOLS,
                on_call=lambda name, args: actions.append(name),
                # Gated by the setting, not withheld outright.
                confirm=_may_run(sess),
            )
        except Exception as e:
            log.exception("Chat turn failed")
            raise HTTPException(500, f"{type(e).__name__}: {e}") from e

        sess.persist()
        after = engine_.get_script()
        # Inside the lock, like the stream path: read after it is released,
        # another turn's new sheets get counted as this one's.
        sheets = engine_.list_sheets()

    return {
        "reply": reply,
        "actions": actions,
        "script_changed": before != after,
        "script": after,
        "tabs": tab_names(after),
        "sheets": sheets,
        "sheets_changed": len(sheets) != sheets_before,
        "file": engine_.app_file_info(),
    }


def _sse(event):
    return f"data: {json.dumps(event, default=str)}\n\n"


@app.post("/api/chat/stream")
def post_chat_stream(body: ChatMessage, sess=Depends(qlik_session),
                     identity: Identity = Depends(require_user)):
    """The same turn, streamed, so a slow local model doesn't look hung."""

    engine_ = _engine(sess)

    try:
        model = sess.ensure_model()
    except OllamaError as e:
        raise HTTPException(503, str(e)) from e

    def events():
        # Everything is inside the try: an exception raised after the agent
        # loop - reading the script back, listing sheets - used to abort the
        # response mid-stream, which the browser reports only as "network
        # error" with nothing to act on.
        try:
            with sess.lock:
                before = engine_.get_script()
                sheets_before = len(engine_.list_sheets())
                messages = sess.messages()
                messages.append({"role": "user", "content": body.message})

                try:
                    for event in stream_agent(
                        sess.client(), model, engine_, messages,
                        allowed=ASSISTANT_TOOLS,
                        confirm=_may_run(sess),
                    ):
                        yield _sse(event)
                finally:
                    # The Stop button closes this generator mid-turn, and
                    # whatever the assistant already built is really built.
                    # Persisting only on a clean finish left those charts on
                    # the sheet with no record of them in the conversation,
                    # so the next turn would answer "what did you build?"
                    # from a history that never saw them. Runs on the way
                    # out either way.
                    sess.persist()

                after = engine_.get_script()
                sheets = engine_.list_sheets()

            yield _sse({
                "type": "final",
                "script_changed": before != after,
                "script": after,
                "tabs": tab_names(after),
                "sheets": sheets,
                "sheets_changed": len(sheets) != sheets_before,
                "file": engine_.app_file_info(),
                # The sidebar needs to learn the new title after the first
                # question, and the id if this turn is what created the chat.
                "chat_id": sess.chat_id(),
                "chats": sess.listing(),
            })
        except Exception as e:
            log.exception("Streamed chat turn failed")
            yield _sse({"type": "error", "message": f"{type(e).__name__}: {e}"})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        # Proxies and browsers otherwise buffer, which defeats the point.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/reset-chat")
def post_reset_chat(sess=Depends(qlik_session)):
    sess.reset_chat()
    return {"ok": True, "chat_id": sess.chat_id()}


# ----------------------------------------------------------------------
# Saved conversations
# ----------------------------------------------------------------------

@app.get("/api/chats")
def get_chats(sess=Depends(qlik_session)):
    """Every saved chat of the person asking, newest first."""
    return {"chats": sess.listing(), "chat_id": sess.chat_id()}


@app.post("/api/chats")
def post_chats(sess=Depends(qlik_session)):
    """Start a new conversation, filing the current one away."""
    sess.reset_chat()
    return {"ok": True, "chat_id": sess.chat_id(), "chats": sess.listing()}


@app.get("/api/chats/{chat_id}")
def get_chat(chat_id: str, sess=Depends(qlik_session)):
    """One saved chat, read-only.

    A GET is fair game for browser prefetching, so it must not touch the
    live conversation - opening is the POST below.

    Only ever reads the asking user's own directory, so a guessed id is not
    a way into somebody else's conversation.
    """
    if not history.valid_id(chat_id):
        raise HTTPException(404, "No such chat.")

    record = history.load(chat_id, sess.owner)
    if record is None:
        raise HTTPException(404, "That chat could not be read.")

    return {
        "chat_id": chat_id,
        "title": record.get("title"),
        "app": record.get("app"),
        "transcript": transcript(record.get("messages")),
    }


@app.post("/api/chats/{chat_id}/open")
def post_open_chat(chat_id: str, sess=Depends(qlik_session)):
    """Reopen a saved chat: restores the model's context and the transcript.

    A POST because it mutates the server: the loaded chat becomes the live
    conversation, and the app it was about is reopened when a different one
    is open now.
    """
    if not history.valid_id(chat_id):
        raise HTTPException(404, "No such chat.")

    was_open = sess.app_name()
    record = sess.load_chat(chat_id)
    if record is None:
        raise HTTPException(404, "That chat could not be read.")

    return {
        "chat_id": chat_id,
        "title": record.get("title"),
        "app": sess.app_name(),
        "app_switched": bool(sess.app_name()) and sess.app_name() != was_open,
        # Set when the chat's app could not be reopened: the transcript is
        # back, but the answers are about a different app than the open one.
        "warning": record.get("app_mismatch"),
        "transcript": transcript(record.get("messages")),
    }


@app.delete("/api/chats/{chat_id}")
def delete_chat(chat_id: str, sess=Depends(qlik_session)):
    if not history.valid_id(chat_id):
        raise HTTPException(404, "No such chat.")
    sess.delete_chat(chat_id)
    return {"ok": True, "chat_id": sess.chat_id(), "chats": sess.listing()}


@app.patch("/api/chats/{chat_id}")
def patch_chat(chat_id: str, rename: Rename, sess=Depends(qlik_session)):
    """Rename a chat. Only its name changes - not the conversation."""
    if not history.valid_id(chat_id):
        raise HTTPException(404, "No such chat.")

    try:
        record = history.rename(chat_id, rename.title, sess.owner)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    if record is None:
        raise HTTPException(404, "That chat could not be read.")

    return {"ok": True, "chat_id": sess.chat_id(), "chats": sess.listing()}


# ----------------------------------------------------------------------
# Administration
# ----------------------------------------------------------------------

@app.get("/admin")
def admin_page(request: Request):
    """The console. Not an API, so a non-admin is sent home rather than 403'd."""
    identity = getattr(request.state, "identity", None)
    if identity is None:
        return RedirectResponse("/login", status_code=303)
    if not identity.is_admin:
        return RedirectResponse("/", status_code=303)
    return FileResponse(ADMIN)


@app.get("/api/admin/users")
def admin_users(identity: Identity = Depends(require_admin)):
    live = {s.key for s in session.sessions()}
    return {
        "users": [
            dict(user, online=(session.key_for(user) in live))
            for user in users.listing()
        ],
        "roles": list(users.ROLES),
        "per_user_engines": session.per_user_engines(),
        "directory_sign_in": directory.enabled(),
    }


@app.post("/api/admin/users")
def admin_create_user(body: NewUser, identity: Identity = Depends(require_admin)):
    try:
        user = users.create(
            body.username, body.password, role=body.role,
            display_name=body.display_name, qlik_directory=body.qlik_directory,
            qlik_user_id=body.qlik_user_id, active=body.active,
        )
    except users.UserError as e:
        raise HTTPException(400, str(e)) from e

    users.audit("user.created", user=identity.user, ip=identity.ip,
                detail={"username": user["username"], "role": user["role"]})
    return {"ok": True, "user": user}


@app.patch("/api/admin/users/{user_id}")
def admin_edit_user(user_id: int, body: UserEdit,
                    identity: Identity = Depends(require_admin)):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(400, "Nothing to change.")

    try:
        user = users.update(user_id, **fields)
    except users.UserError as e:
        raise HTTPException(400, str(e)) from e

    # The session is holding a socket opened as whoever they used to be, and
    # a conversation about apps they may no longer be able to see.
    if {"qlik_directory", "qlik_user_id", "active"} & set(fields):
        session.drop(user)

    users.audit("user.updated", user=identity.user, ip=identity.ip,
                detail={"username": user["username"], **fields})
    return {"ok": True, "user": user}


@app.delete("/api/admin/users/{user_id}")
def admin_delete_user(user_id: int, identity: Identity = Depends(require_admin)):
    user = users.get(user_id)
    if user is None:
        raise HTTPException(404, "No such user.")
    if user["id"] == identity.user.get("id"):
        raise HTTPException(400, "You cannot delete the account you are signed in as.")

    try:
        users.delete(user_id)
    except users.UserError as e:
        raise HTTPException(400, str(e)) from e

    session.drop(user)
    # Their conversations are deliberately left on disk. Deleting an account
    # is a personnel change; destroying the record of what was asked of a
    # bank's data is a different decision, and not one to take as a side
    # effect of this button.
    users.audit("user.deleted", user=identity.user, ip=identity.ip,
                detail={"username": user["username"],
                        "history_kept": history.owner_key(user_id)})
    return {"ok": True}


@app.post("/api/admin/users/{user_id}/password")
def admin_set_password(user_id: int, body: NewPassword,
                       identity: Identity = Depends(require_admin)):
    user = users.get(user_id)
    if user is None:
        raise HTTPException(404, "No such user.")
    try:
        users.set_password(user_id, body.password)
    except users.UserError as e:
        raise HTTPException(400, str(e)) from e

    # Everywhere they are signed in now was signed in with the old password.
    users.end_sessions(user_id)
    users.audit("password.reset", user=identity.user, ip=identity.ip,
                detail={"username": user["username"]})
    return {"ok": True}


@app.post("/api/admin/users/{user_id}/unlock")
def admin_unlock(user_id: int, identity: Identity = Depends(require_admin)):
    user = users.get(user_id)
    if user is None:
        raise HTTPException(404, "No such user.")
    users.unlock(user_id)
    users.audit("user.unlocked", user=identity.user, ip=identity.ip,
                detail={"username": user["username"]})
    return {"ok": True}


@app.post("/api/admin/users/{user_id}/token")
def admin_mint_token(user_id: int, identity: Identity = Depends(require_admin)):
    """Mint an MCP token. Shown once - there is nowhere to read it back."""
    try:
        token = users.mint_token(user_id)
    except users.UserError as e:
        raise HTTPException(404, str(e)) from e
    users.audit("token.minted", user=identity.user, ip=identity.ip,
                detail={"username": (users.get(user_id) or {}).get("username")})
    return {"ok": True, "token": token}


@app.delete("/api/admin/users/{user_id}/token")
def admin_revoke_token(user_id: int, identity: Identity = Depends(require_admin)):
    users.revoke_token(user_id)
    users.audit("token.revoked", user=identity.user, ip=identity.ip,
                detail={"username": (users.get(user_id) or {}).get("username")})
    return {"ok": True}


@app.get("/api/admin/chats")
def admin_chats(user_id: int | None = None,
                identity: Identity = Depends(require_admin)):
    """Everyone's conversations, or one person's.

    Reading someone's chat is itself recorded. The people whose questions
    these are cannot see this page, so the only thing keeping it honest is
    that looking leaves a mark.
    """
    if user_id:
        user = users.get(user_id)
        if user is None:
            raise HTTPException(404, "No such user.")
        chats = history.listing(history.owner_key(user_id))
    else:
        chats = history.all_listing()

    known = {history.owner_key(u["id"]): u for u in users.listing()}
    for chat in chats:
        person = known.get(chat.get("owner"))
        chat["username"] = person["username"] if person else chat.get("owner") or "—"
        chat["display_name"] = (person or {}).get("display_name") or ""

    return {"chats": chats, "users": users.listing()}


@app.get("/api/admin/chats/{owner}/{chat_id}")
def admin_read_chat(owner: str, chat_id: str,
                    identity: Identity = Depends(require_admin)):
    if not history.valid_owner(owner) or not history.valid_id(chat_id):
        raise HTTPException(404, "No such chat.")

    record = history.load(chat_id, owner)
    if record is None:
        raise HTTPException(404, "That chat could not be read.")

    users.audit("chat.read", user=identity.user, ip=identity.ip,
                detail={"owner": owner, "chat": chat_id,
                        "title": record.get("title")})

    return {
        "chat_id": chat_id,
        "owner": owner,
        "title": record.get("title"),
        "app": record.get("app"),
        "created": record.get("created"),
        "updated": record.get("updated"),
        "transcript": transcript(record.get("messages")),
    }


@app.get("/api/admin/sessions")
def admin_sessions(identity: Identity = Depends(require_admin)):
    users.purge_expired()
    return {
        "sessions": users.active_sessions(),
        # `connected` is here so the person on call can see which sessions
        # actually have a Qlik connection right now. Without it the page
        # showed which app each person had open whether or not the socket
        # behind it still existed.
        "qlik": [
            {"key": s.key, "username": s.username, "app": s.app_name(),
             "impersonates": s.impersonates, "chat_id": s.chat_id(),
             "connected": s.connected(), "error": s.last_error}
            for s in session.sessions()
        ],
    }


@app.delete("/api/admin/sessions/{handle}")
def admin_end_session(handle: str, identity: Identity = Depends(require_admin)):
    if not users.end_session_by_handle(handle):
        raise HTTPException(404, "No such session.")
    users.audit("session.ended", user=identity.user, ip=identity.ip,
                detail={"handle": handle})
    return {"ok": True}


@app.get("/api/admin/audit")
def admin_audit(limit: int = 200, user_id: int | None = None,
                action: str | None = None,
                identity: Identity = Depends(require_admin)):
    return {
        "entries": users.audit_trail(limit=limit, user_id=user_id, action=action),
        "actions": users.audit_actions(),
        "users": users.listing(),
    }


@app.post("/api/admin/act-as")
def admin_act_as(body: Impersonation, identity: Identity = Depends(require_user)):
    """Stand in for a user to reproduce what they are seeing, or stop.

    Everything done from here happens under that user's Qlik identity and in
    their conversation, which is the entire point - and the reason both ends
    of it are audited and the page wears a banner throughout.

    This is the one administrator's endpoint that does NOT go through
    require_admin, and it has to be: while standing in for an ordinary user
    you are refused administrator's powers, so a stop button behind that
    check could never be pressed. The way out of a borrowed identity cannot
    require the identity you have put down. The *real* account is what is
    checked instead.
    """
    if not identity.user.get("is_admin"):
        raise HTTPException(403, "Administrators only.")

    if not identity.token:
        raise HTTPException(
            400, "Acting as another user needs a browser session, not an API token.")

    if body.user_id is None:
        users.set_acting_as(identity.token, None)
        users.audit("act-as.stopped", user=identity.user, ip=identity.ip,
                    detail={"was": (identity.acting_as or {}).get("username")})
        return {"ok": True, "acting_as": None}

    if identity.impersonating:
        raise HTTPException(
            400, "You are already acting as another user. Stop first.")

    target = users.get(body.user_id)
    if target is None or not target["active"]:
        raise HTTPException(404, "No such active user.")
    if target["id"] == identity.user.get("id"):
        raise HTTPException(400, "You are already signed in as that account.")

    users.set_acting_as(identity.token, target["id"])
    users.audit("act-as.started", user=identity.user, ip=identity.ip,
                detail={"username": target["username"]})
    return {"ok": True, "acting_as": target["username"]}


# The same tools, same session, over MCP - so Claude Code and the browser are
# looking at one open app rather than competing for it. The gate middleware
# covers this mount: without it, /mcp was the one door with no lock, and it
# can rewrite the load script and reload the data.
#
# streamable_http_path="/" because the sub-application mounts its own route
# at "/mcp" by default, and mounting THAT at "/mcp" puts the endpoint on
# "/mcp/mcp" - while this file's banner, the README and every example config
# say "/mcp". A client pointed where the documentation sends it got a 307 to
# a 404, which reads as the server being broken rather than as an address
# being wrong.
app.mount("/mcp", mcp.streamable_http_app(streamable_http_path="/"))


# ----------------------------------------------------------------------


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--app", default=APP_NAME)
    parser.add_argument("--model", default=CHAT_MODEL)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    return parser.parse_args(argv)


def _loopback(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host in ("localhost", "")


def _apply_retention():
    """Enforce the retention policy at startup.

    Here rather than on a timer, because a long-running service that is
    restarted for patching gets this often enough, and a background thread
    deleting a bank's audit trail on its own schedule is a worse idea than a
    predictable one. `manage_users.py prune` runs it on demand.
    """
    try:
        records = users.prune_audit()
        chats = history.prune_old()
    except Exception:
        # Never fatal to starting: losing the ability to serve because a
        # deletion failed would be the wrong trade.
        log.exception("Could not apply the retention policy")
        return
    if records or chats:
        print(f"  retention    -> removed {records} audit record(s), "
              f"{chats} conversation(s)", flush=True)


def _adopt_earlier_conversations():
    """Give chats saved before there were accounts to the first administrator.

    They were written straight into the history root by the single-operator
    version. Every listing now reads a per-person directory, so left where
    they are they would simply vanish - which looks exactly like the upgrade
    having deleted them.
    """
    owner = users.by_username(users.ADMIN_USERNAME)
    if owner is None:
        # The configured administrator was renamed or removed. Any active
        # admin will do; the point is that the conversations stay reachable.
        admins = [u for u in users.listing() if u["is_admin"] and u["active"]]
        if not admins:
            return
        owner = admins[0]

    moved = history.adopt_loose_chats(history.owner_key(owner["id"]))
    if moved:
        print(f"  moved {moved} earlier conversation(s) to {owner['username']}",
              flush=True)


def configure_logging():
    """Console for a person watching, a rotating file for a service.

    A Windows service has no console, so without a file the only record of a
    problem is whatever the service wrapper happened to catch on its way past.
    Rotation matters for the same reason nobody notices it: a server that runs
    for a year fills a disk exactly once.

    The console keeps the bare format the startup banner is written in; the
    file gets timestamps and logger names, because it is read long after the
    fact by somebody who was not there.

    This is the operational log - what the server did. *Who* did what is the
    audit trail in the accounts database, deliberately somewhere else with
    different retention needs.
    """
    level = getattr(logging, LOG_LEVEL, logging.INFO)

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(logging.Formatter("%(message)s"))
    handlers = [console]

    if LOG_FILE:
        path = Path(LOG_FILE).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        rotating = RotatingFileHandler(
            path, maxBytes=LOG_MAX_MB * 1024 * 1024,
            backupCount=LOG_BACKUPS, encoding="utf-8",
        )
        rotating.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        handlers.append(rotating)

    # force=True: importing mcp_server above already configured logging for
    # its stdio transport, which made this call a silent no-op - the plain
    # format below never actually applied.
    logging.basicConfig(level=level, handlers=handlers, force=True)

    # httpx logs every model call at INFO, which buries our own output and,
    # on a busy server, is most of the log by volume.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    return LOG_FILE


def main(argv=None):
    args = parse_args(argv)
    configure_logging()

    for page in (INDEX, LOGIN, ADMIN):
        if not page.is_file():
            print(f"Missing {page}", file=sys.stderr)
            return 1

    # Binding to a real interface with no login puts the bank's figures, the
    # load script and a reload button on the network for anyone who can
    # reach the port. That was previously contained only by the default
    # host, with nothing to warn the first person who changed it.
    if not AUTH_ENABLED and not _loopback(args.host):
        print(
            f"Refusing to serve {args.host} with AUTH_ENABLED=false.\n"
            "There would be no login in front of the data, the load script "
            "or the reload button.\nSet AUTH_ENABLED=true, or bind to "
            "127.0.0.1.",
            file=sys.stderr,
        )
        return 1

    if AUTH_ENABLED:
        try:
            generated = users.bootstrap()
            _adopt_earlier_conversations()
        except Exception as e:
            print(f"Could not open the account database: {e}", file=sys.stderr)
            return 1
        if generated:
            print("\n  ---------------------------------------------------")
            print("  First run: an administrator account was created.")
            print(f"  username: {users.ADMIN_USERNAME}")
            print(f"  password: {generated}")
            print("  This is shown once. Change it after signing in.")
            print("  ---------------------------------------------------")

    # Qlik used to be a condition of starting at all: unreachable engine,
    # exit code 1, no server. Under a service manager that is a restart loop,
    # and it takes down everything that has nothing to do with Qlik - the
    # login page, the admin pages, every saved conversation, the audit trail.
    # A Qlik restart became a total outage that needed a human.
    #
    # So it is now a warning, and the connection is opened on the first
    # request that needs it. REQUIRE_QLIK_AT_STARTUP puts the old behaviour
    # back for anyone who would rather fail fast.
    engine_ = None
    try:
        engine_ = session.open_app(args.app)
    except QlikEngineError as e:
        if REQUIRE_QLIK_AT_STARTUP:
            print(f"Can't open {args.app!r}: {e}", file=sys.stderr)
            return 1
        print(f"  Qlik unavailable: {e}", file=sys.stderr)
        print("  Starting anyway - sign-in and saved chats work, and the "
              "connection is retried on the first request.", flush=True)

    # Ollama is not. The editor, the data model and Load data all work
    # without it, so a stopped Ollama should cost you the assistant - not the
    # whole product. It is picked up automatically once it starts.
    model = None
    try:
        model, note = session.resolve_model(args.model)
        if note:
            print(f"  note: {note}", flush=True)
    except (OllamaError, QlikEngineError) as e:
        print(f"  assistant unavailable: {e}", flush=True)

    print(f"\n  Qlik AI      -> http://{args.host}:{args.port}", flush=True)
    print(f"  Admin        -> http://{args.host}:{args.port}/admin", flush=True)
    print(f"  MCP endpoint -> http://{args.host}:{args.port}/mcp", flush=True)
    print(
        f"  app {args.app!r} "
        f"({engine_.mode if engine_ else 'Qlik not reachable yet'}), "
        f"model {model or 'none - start Ollama and just ask again'}",
        flush=True,
    )
    print(
        "  sign-in required\n" if AUTH_ENABLED
        else "  AUTH_ENABLED=false - no sign-in, loopback only\n",
        flush=True,
    )

    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    finally:
        session.close_all()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
