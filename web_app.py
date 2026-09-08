import argparse
import contextlib
import ipaddress
import json
import logging
import queue
import sys
import threading
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
    PASSWORD_MIN,
    WORKER_THREADS,
)
from data_prep import tab_names
from mcp_server import mcp
from llm import ModelError, describe_models
from qlik_engine import QlikEngineError

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
INDEX = HERE / "web" / "index.html"
LOGIN = HERE / "web" / "login.html"
ADMIN = HERE / "web" / "admin.html"

ASSISTANT_TOOLS = LOAD_EDITOR_TOOLS | {
    "build_dashboard", "create_chart", "list_charts", "edit_chart", "check_expression",
    "analyze_sheet", "save", "open_app", "reload_data",
}


def _may_run(sess):
    def may(question, action):
        return action == "reload_data" and sess.allow_reload()
    return may


def widen_the_threadpool():
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

    async with contextlib.AsyncExitStack() as stack:
        await stack.enter_async_context(mcp.session_manager.run())
        yield
    session.close_all()
    llm.close_shared()


app = FastAPI(title="Qlik AI", lifespan=lifespan)


@app.middleware("http")
async def _gate(request: Request, call_next):
    return await auth.gate(request, call_next)


def _engine(sess):
    try:
        if sess.app_name() is None:
            sess.open_app(APP_NAME)
        return sess.engine()
    except session.QlikIdentityMissing as e:
        raise HTTPException(403, str(e)) from e
    except session.QlikConnectionLost as e:
        raise HTTPException(503, str(e), headers={"X-Qlik-Reconnect": "1"}) from e
    except QlikEngineError as e:
        raise HTTPException(503, str(e)) from e


def transcript(messages):
    out = []
    for message in messages or []:
        role = message.get("role")
        content = (message.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            out.append({"role": role, "content": content})
    return out


class ScriptUpdate(BaseModel):
    content: str


class ChatMessage(BaseModel):
    message: str


class Selection(BaseModel):
    app: str = ""
    model: str = ""
    allow_reload: bool | None = None
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


@app.get("/login")
def login_page(request: Request):
    if getattr(request.state, "identity", None) is not None:
        return RedirectResponse("/", status_code=303)
    return FileResponse(LOGIN)


@app.get("/api/session")
def get_session(request: Request):
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
            raise HTTPException(
                503,
                "The directory could not be reached, so your Windows password "
                "could not be checked. Try again shortly, or tell whoever "
                "runs this server.",
            )
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


@app.get("/")
def index():
    return FileResponse(INDEX)


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/health")
def get_health(sess=Depends(qlik_session), identity: Identity = Depends(require_user)):
    return sess.health()


@app.post("/api/reconnect")
def post_reconnect(sess=Depends(qlik_session), identity: Identity = Depends(require_user)):
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


@app.get("/api/admin/qlik-users")
def get_qlik_users(identity: Identity = Depends(require_admin)):
    import qrs

    if not qrs.enabled():
        raise HTTPException(400, "QRS_ENABLED is false, so there is no "
                                 "repository to read accounts from.")
    try:
        rows = qrs.fetch_users()
    except Exception as e:
        raise HTTPException(502, str(e)) from e

    people = []
    for row in rows:
        directory_name, user_id = qrs.identity(row)
        people.append({
            "directory": directory_name,
            "user_id": user_id,
            "name": (row.get("name") or "").strip(),
            "admin": qrs.is_admin(row),
            "skipped": qrs.skipped(row),
        })
    return {"where": qrs.where(), "users": people}


@app.post("/api/admin/sync-users")
def post_sync_users(identity: Identity = Depends(require_admin)):
    import qrs

    if not qrs.enabled():
        raise HTTPException(400, "QRS_ENABLED is false, so there is no "
                                 "repository to read accounts from.")
    try:
        outcome = qrs.sync()
    except Exception as e:
        raise HTTPException(502, str(e)) from e

    users.audit("users.synced", user=identity.user, ip=identity.ip,
                detail=identity.audited(
                    created=len(outcome["created"]),
                    updated=len(outcome["updated"]),
                    skipped=len(outcome["skipped"]),
                    failed=len(outcome["failed"])))

    return {
        "where": qrs.where(),
        "created": [{"username": n, "qlik": w, "role": r}
                    for n, w, r in outcome["created"]],
        "updated": [{"username": n, "qlik": w, "fields": f}
                    for n, w, f in outcome["updated"]],
        "unchanged": len(outcome["unchanged"]),
        "skipped": [{"qlik": w, "why": y} for w, y in outcome["skipped"]],
        "failed": [{"username": n, "error": e} for n, e in outcome["failed"]],
        "passwords": outcome["passwords"],
    }


@app.get("/api/admin/health")
def get_admin_health(identity: Identity = Depends(require_admin)):
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
        "model_host": llm.where(),
        "chat_id": sess.chat_id(),
        "transcript": transcript(sess.messages()),
        "user": identity.summary(),
    }


@app.get("/api/options")
def get_options(sess=Depends(qlik_session)):
    try:
        apps = sess.list_apps()
    except QlikEngineError as e:
        log.debug("Could not list apps: %s", e)
        apps = []

    open_now = sess.app_name()
    if open_now and open_now not in apps:
        apps.insert(0, open_now)

    try:
        models = describe_models(sess.client())
    except ModelError as e:
        log.debug("Could not list models: %s", e)
        models = [{
            "name": sess.model() or f"{llm.where()} not reachable",
            "usable": sess.assistant_ready(),
            "reason": "" if sess.assistant_ready()
                      else f"could not reach {llm.where()}",
            "size_gb": 0,
        }]

    return {
        "apps": apps, "app": sess.app_name(),
        "models": models, "model": sess.model(),
    }


@app.post("/api/select")
def post_select(selection: Selection, sess=Depends(qlik_session),
                identity: Identity = Depends(require_user)):
    if selection.allow_reload is not None:
        sess.set_allow_reload(selection.allow_reload)

    if selection.language is not None:
        sess.set_language(selection.language)

    if selection.model and selection.model != sess.model():
        try:
            sess.set_model(selection.model)
        except ModelError as e:
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


@app.put("/api/script")
def put_script(update: ScriptUpdate, sess=Depends(qlik_session),
               identity: Identity = Depends(require_user)):
    try:
        _engine(sess).set_script(update.content)
    except QlikEngineError as e:
        raise HTTPException(400, str(e)) from e
    users.audit("script.saved", user=identity.effective, ip=identity.ip,
                detail=identity.audited(app=sess.app_name(),
                                        characters=len(update.content)))
    return {"ok": True, "tabs": tab_names(update.content)}


@app.post("/api/reload")
def post_reload(sess=Depends(qlik_session), identity: Identity = Depends(require_user)):
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
    return {"ok": True, "file": engine_.app_file_info()}


@app.post("/api/chat")
def post_chat(body: ChatMessage, sess=Depends(qlik_session),
              identity: Identity = Depends(require_user)):
    engine_ = _engine(sess)
    actions = []

    try:
        model = sess.ensure_model()
    except ModelError as e:
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
                confirm=_may_run(sess),
            )
        except Exception as e:
            log.exception("Chat turn failed")
            raise HTTPException(500, f"{type(e).__name__}: {e}") from e

        sess.persist()
        after = engine_.get_script()
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
    engine_ = _engine(sess)

    try:
        model = sess.ensure_model()
    except ModelError as e:
        raise HTTPException(503, str(e)) from e

    outbox = queue.Queue(maxsize=64)
    cancelled = threading.Event()
    FINISHED = object()

    def emit(event):
        while not cancelled.is_set():
            try:
                outbox.put(event, timeout=0.2)
                return True
            except queue.Full:
                continue
        return False

    def turn():
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
                        if not emit(event):
                            break
                finally:
                    sess.persist()

                if cancelled.is_set():
                    return
                after = engine_.get_script()
                sheets = engine_.list_sheets()

            emit({
                "type": "final",
                "script_changed": before != after,
                "script": after,
                "tabs": tab_names(after),
                "sheets": sheets,
                "sheets_changed": len(sheets) != sheets_before,
                "file": engine_.app_file_info(),
                "chat_id": sess.chat_id(),
                "chats": sess.listing(),
            })
        except Exception as e:
            log.exception("Streamed chat turn failed")
            emit({"type": "error", "message": f"{type(e).__name__}: {e}"})
        finally:
            outbox.put(FINISHED)

    def events():
        worker = threading.Thread(target=turn, name=f"chat-{sess.key}",
                                  daemon=True)
        worker.start()
        try:
            while True:
                event = outbox.get()
                if event is FINISHED:
                    return
                yield _sse(event)
        finally:
            cancelled.set()

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/reset-chat")
def post_reset_chat(sess=Depends(qlik_session)):
    sess.reset_chat()
    return {"ok": True, "chat_id": sess.chat_id()}


@app.get("/api/chats")
def get_chats(sess=Depends(qlik_session)):
    return {"chats": sess.listing(), "chat_id": sess.chat_id()}


@app.post("/api/chats")
def post_chats(sess=Depends(qlik_session)):
    sess.reset_chat()
    return {"ok": True, "chat_id": sess.chat_id(), "chats": sess.listing()}


@app.get("/api/chats/{chat_id}")
def get_chat(chat_id: str, sess=Depends(qlik_session)):
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
    if not history.valid_id(chat_id):
        raise HTTPException(404, "No such chat.")

    try:
        record = history.rename(chat_id, rename.title, sess.owner)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    if record is None:
        raise HTTPException(404, "That chat could not be read.")

    return {"ok": True, "chat_id": sess.chat_id(), "chats": sess.listing()}


@app.get("/admin")
def admin_page(request: Request):
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


app.mount("/mcp", mcp.streamable_http_app(streamable_http_path="/"))


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Qlik + local AI, in one place.")
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
    try:
        records = users.prune_audit()
        chats = history.prune_old()
    except Exception:
        log.exception("Could not apply the retention policy")
        return
    if records or chats:
        print(f"  retention    -> removed {records} audit record(s), "
              f"{chats} conversation(s)", flush=True)


def _adopt_earlier_conversations():
    owner = users.by_username(users.ADMIN_USERNAME)
    if owner is None:
        admins = [u for u in users.listing() if u["is_admin"] and u["active"]]
        if not admins:
            return
        owner = admins[0]

    moved = history.adopt_loose_chats(history.owner_key(owner["id"]))
    if moved:
        print(f"  moved {moved} earlier conversation(s) to {owner['username']}",
              flush=True)


def configure_logging():
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

    logging.basicConfig(level=level, handlers=handlers, force=True)

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
        except users.UserError as e:
            print(f"Could not create the administrator "
                  f"{users.ADMIN_USERNAME!r}: {e}\n"
                  f"ADMIN_PASSWORD in .env has to satisfy PASSWORD_MIN "
                  f"(currently {PASSWORD_MIN}). Lower PASSWORD_MIN, or "
                  f"set a longer ADMIN_PASSWORD.", file=sys.stderr)
            return 1
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

    model = None
    try:
        model, note = session.resolve_model(args.model)
        if note:
            print(f"  note: {note}", flush=True)
    except (ModelError, QlikEngineError) as e:
        print(f"  assistant unavailable: {e}", flush=True)

    print(f"\n  Qlik AI      -> http://{args.host}:{args.port}", flush=True)
    print(f"  Admin        -> http://{args.host}:{args.port}/admin", flush=True)
    print(f"  MCP endpoint -> http://{args.host}:{args.port}/mcp", flush=True)
    print(
        f"  app {args.app!r} "
        f"({engine_.mode if engine_ else 'Qlik not reachable yet'}), "
        f"model {model or f'none - {llm.where()} was not reachable'}",
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
