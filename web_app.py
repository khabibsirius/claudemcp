"""Qlik + local AI, in one place.

    python web_app.py

Opens one server that is the whole product:

    http://127.0.0.1:8000        the load editor, with the assistant
                                 underneath it
    http://127.0.0.1:8000/mcp    the same session over MCP, for Claude
                                 Code / Claude Desktop

One Qlik connection is shared by all of it (see session.py), so switching
app in the browser switches it for the MCP client too, and neither locks the
other out - which matters because Qlik Sense Desktop allows only one session
per app.

Nothing leaves the machine: browser -> this server -> Qlik and Ollama.
"""

import argparse
import contextlib
import json
import logging
import sys
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

import history
import session
from chat_tools import LOAD_EDITOR_TOOLS, run_agent, stream_agent
from config import APP_NAME, CHAT_MODEL
from data_prep import tab_names
from mcp_server import mcp
from ollama_client import OllamaError, describe_models
from qlik_engine import QlikEngineError

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
INDEX = HERE / "web" / "index.html"

# Everything, including reloading. Reloading is gated by a setting rather
# than withheld: a model that is simply refused concludes the action is
# impossible and tells the user it cannot be done, which turns "load my data
# and chart it" into a dead end.
ASSISTANT_TOOLS = LOAD_EDITOR_TOOLS | {
    "build_dashboard", "create_chart", "list_charts", "edit_chart", "check_expression",
    "save", "open_app", "reload_data",
}


@contextlib.asynccontextmanager
async def lifespan(_app):
    # The MCP session manager needs its own lifespan running, or /mcp 500s on
    # first use.
    async with contextlib.AsyncExitStack() as stack:
        await stack.enter_async_context(mcp.session_manager.run())
        yield
    session.close()


app = FastAPI(title="Qlik AI", lifespan=lifespan)


def engine():
    try:
        return session.engine()
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


@app.get("/")
def index():
    return FileResponse(INDEX)


@app.get("/api/state")
def get_state():
    """Everything the page needs: script, tabs, connections, sheets."""
    engine_ = engine()
    script = engine_.get_script()

    def safely(call, default):
        try:
            return call()
        except QlikEngineError as e:
            log.debug("%s unavailable: %s", call, e)
            return default

    return {
        "app": session.app_name(),
        "model": session.model(),
        "mode": engine_.mode,
        "script": script,
        "tabs": tab_names(script),
        "connections": safely(engine_.list_connections, []),
        "sheets": safely(engine_.list_sheets, []),
        "has_data": bool(safely(engine_.get_fields, [])),
        "allow_reload": session.allow_reload(),
        "assistant_ready": session.assistant_ready(),
        "chat_id": session.chat_id(),
        "transcript": transcript(session.messages()),
    }


@app.get("/api/options")
def get_options():
    """Apps and models the person can switch between."""
    try:
        apps = [a["name"] for a in engine().list_apps() if a["name"]]
    except QlikEngineError as e:
        log.debug("Could not list apps: %s", e)
        apps = [session.app_name()]

    try:
        models = describe_models(session.client())
    except OllamaError as e:
        log.debug("Could not list models: %s", e)
        # Ollama is down. Say so in the dropdown rather than showing an
        # empty list that looks like a bug in this app.
        models = [{
            "name": session.model() or "Ollama not running",
            "usable": session.assistant_ready(),
            "reason": "" if session.assistant_ready() else "start Ollama, then reload this page",
            "size_gb": 0,
        }]

    return {
        "apps": apps, "app": session.app_name(),
        "models": models, "model": session.model(),
    }


@app.post("/api/select")
def post_select(selection: Selection):
    """Switch app, model or the reload setting without restarting."""
    if selection.allow_reload is not None:
        session.set_allow_reload(selection.allow_reload)

    if selection.model and selection.model != session.model():
        try:
            session.set_model(selection.model)
        except OllamaError as e:
            raise HTTPException(400, str(e)) from e

    if selection.app and selection.app != session.app_name():
        try:
            session.open_app(selection.app)
        except QlikEngineError as e:
            raise HTTPException(400, str(e)) from e

    return {
        "ok": True, "app": session.app_name(), "model": session.model(),
        "allow_reload": session.allow_reload(),
    }


# ----------------------------------------------------------------------
# Data load editor
# ----------------------------------------------------------------------

@app.put("/api/script")
def put_script(update: ScriptUpdate):
    """Save the script as edited in the browser.

    Syntax-checked by the engine; one that doesn't parse is rejected and the
    previous script stays, so the editor can't leave the app unable to run.
    """
    try:
        engine().set_script(update.content)
    except QlikEngineError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True, "tabs": tab_names(update.content)}


@app.post("/api/reload")
def post_reload():
    """Run the load script, from the Load data button."""
    engine_ = engine()
    result = engine_.reload_data()
    if result.get("success"):
        result["tables"] = [
            {"name": t["name"], "rows": t["rows"]} for t in engine_.get_tables()
        ]
        result["total_rows"] = sum(t["rows"] for t in result["tables"])
    return result


@app.post("/api/save")
def post_save():
    engine_ = engine()
    engine_.save()
    # The file's timestamp is proof the change reached disk, which is what
    # tells "it didn't save" apart from "Qlik is showing you a cached copy".
    return {"ok": True, "file": engine_.app_file_info()}


# ----------------------------------------------------------------------
# Assistant
# ----------------------------------------------------------------------

@app.post("/api/chat")
def post_chat(body: ChatMessage):
    """One turn with the assistant, then report what changed."""
    engine_ = engine()
    actions = []

    try:
        model = session.ensure_model()
    except OllamaError as e:
        raise HTTPException(503, str(e)) from e

    with session.lock():
        before = engine_.get_script()
        sheets_before = len(engine_.list_sheets())
        messages = session.messages()
        messages.append({"role": "user", "content": body.message})

        try:
            reply = run_agent(
                session.client(), model, engine_, messages,
                allowed=ASSISTANT_TOOLS,
                on_call=lambda name, args: actions.append(name),
                # Gated by the setting, not withheld outright.
                confirm=lambda question: session.allow_reload(),
            )
        except Exception as e:
            log.exception("Chat turn failed")
            raise HTTPException(500, f"{type(e).__name__}: {e}") from e

        session.persist()
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
def post_chat_stream(body: ChatMessage):
    """The same turn, streamed, so a slow local model doesn't look hung."""

    engine_ = engine()

    try:
        model = session.ensure_model()
    except OllamaError as e:
        raise HTTPException(503, str(e)) from e

    def events():
        # Everything is inside the try: an exception raised after the agent
        # loop - reading the script back, listing sheets - used to abort the
        # response mid-stream, which the browser reports only as "network
        # error" with nothing to act on.
        try:
            with session.lock():
                before = engine_.get_script()
                sheets_before = len(engine_.list_sheets())
                messages = session.messages()
                messages.append({"role": "user", "content": body.message})

                for event in stream_agent(
                    session.client(), model, engine_, messages,
                    allowed=ASSISTANT_TOOLS,
                    confirm=lambda question: session.allow_reload(),
                ):
                    yield _sse(event)

                session.persist()
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
                "chat_id": session.chat_id(),
                "chats": history.listing(),
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
def post_reset_chat():
    session.reset_chat()
    return {"ok": True, "chat_id": session.chat_id()}


# ----------------------------------------------------------------------
# Saved conversations
# ----------------------------------------------------------------------

@app.get("/api/chats")
def get_chats():
    """Every saved chat, newest first, plus which one is open."""
    return {"chats": history.listing(), "chat_id": session.chat_id()}


@app.post("/api/chats")
def post_chats():
    """Start a new conversation, filing the current one away."""
    session.reset_chat()
    return {"ok": True, "chat_id": session.chat_id(), "chats": history.listing()}


@app.get("/api/chats/{chat_id}")
def get_chat(chat_id: str):
    """Reopen a saved chat: restores the model's context and the transcript."""
    if not history.valid_id(chat_id):
        raise HTTPException(404, "No such chat.")

    record = session.load_chat(chat_id)
    if record is None:
        raise HTTPException(404, "That chat could not be read.")

    return {
        "chat_id": chat_id,
        "title": record.get("title"),
        "app": record.get("app"),
        "transcript": transcript(record.get("messages")),
    }


@app.delete("/api/chats/{chat_id}")
def delete_chat(chat_id: str):
    if not history.valid_id(chat_id):
        raise HTTPException(404, "No such chat.")
    session.delete_chat(chat_id)
    return {"ok": True, "chat_id": session.chat_id(), "chats": history.listing()}


# The same tools, same session, over MCP - so Claude Code and the browser are
# looking at one open app rather than competing for it.
app.mount("/mcp", mcp.streamable_http_app())


# ----------------------------------------------------------------------


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--app", default=APP_NAME)
    parser.add_argument("--model", default=CHAT_MODEL)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(message)s")
    # httpx logs every Ollama call at INFO, which buries our own output.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if not INDEX.is_file():
        print(f"Missing {INDEX}", file=sys.stderr)
        return 1

    # Qlik is the hard requirement; without it there is nothing to show.
    try:
        engine_ = session.open_app(args.app)
    except QlikEngineError as e:
        print(f"Can't open {args.app!r}: {e}", file=sys.stderr)
        return 1

    # Ollama is not. The editor, the data model and Load data all work
    # without it, so a stopped Ollama should cost you the assistant - not the
    # whole product. It is picked up automatically once it starts.
    model = None
    try:
        model, note = session.resolve_model(args.model)
        if note:
            print(f"  note: {note}", flush=True)
    except OllamaError as e:
        print(f"  assistant unavailable: {e}", flush=True)

    print(f"\n  Qlik AI      -> http://{args.host}:{args.port}", flush=True)
    print(f"  MCP endpoint -> http://{args.host}:{args.port}/mcp", flush=True)
    print(
        f"  app {args.app!r} ({engine_.mode}), "
        f"model {model or 'none - start Ollama and just ask again'}\n",
        flush=True,
    )

    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    finally:
        session.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
