"""Saved conversations, one JSON file each.

The assistant's memory is the message list in session.py, and until now it
lived only in the server process: refreshing the browser lost the transcript,
and stopping the server lost the conversation. This module writes that same
list to disk after every turn so a chat can be reopened later with the model's
context intact - reloading the pixels without the messages would give the
assistant amnesia while the screen showed a full conversation.

Files rather than a database: a conversation is only ever read or written
whole, there is exactly one writer, and a directory of readable JSON is
something the user can back up, diff or delete by hand.
"""

import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from config import HISTORY_DIR, HISTORY_MAX

log = logging.getLogger(__name__)

# Ids are built here and then handed back to us by the browser, so they are
# validated on the way in as well: anything outside this alphabet could walk
# out of the history directory when joined to a path.
ID_PATTERN = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{4}$")

TITLE_CHARS = 60


def directory():
    """The history directory, created on first use."""
    path = Path(HISTORY_DIR).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def new_id():
    """A sortable id: the filename alone puts the newest chat last."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{os.urandom(2).hex()}"


def valid_id(chat_id):
    return bool(chat_id) and bool(ID_PATTERN.match(str(chat_id)))


def _path(chat_id):
    if not valid_id(chat_id):
        raise ValueError(f"Not a chat id: {chat_id!r}")
    return directory() / f"{chat_id}.json"


def title_from(messages):
    """Name a chat after the first thing that was asked of it."""
    for message in messages:
        if message.get("role") == "user":
            text = " ".join((message.get("content") or "").split())
            if text:
                return text[:TITLE_CHARS] + ("…" if len(text) > TITLE_CHARS else "")
    return "New chat"


def turns(messages):
    """How many questions were asked - the system prompt is not a turn."""
    return sum(1 for m in messages if m.get("role") == "user")


def save(chat_id, messages, app_name=None, title=None):
    """Write a conversation, replacing any previous version of it.

    Written to a temporary file and renamed, because the alternative - opening
    the real file for writing - truncates it first, so a crash mid-write would
    leave an empty file where the conversation used to be.
    """
    if not turns(messages):
        # Nothing was ever asked. Saving would litter the sidebar with empty
        # chats every time the app is opened or switched.
        return None

    path = _path(chat_id)
    record = {
        "id": chat_id,
        "title": title or title_from(messages),
        "app": app_name,
        "created": created_at(chat_id) or _now(),
        "updated": _now(),
        "messages": messages,
    }

    handle, temporary = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(record, out, ensure_ascii=False, indent=1, default=str)
        os.replace(temporary, path)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise

    prune()
    return record


def load(chat_id):
    """One conversation, or None if it is missing or unreadable."""
    try:
        with _path(chat_id).open(encoding="utf-8") as handle:
            record = json.load(handle)
    except (OSError, ValueError) as e:
        log.warning("Could not read chat %s: %s", chat_id, e)
        return None

    if not isinstance(record, dict) or not isinstance(record.get("messages"), list):
        log.warning("Chat %s is not in the expected shape", chat_id)
        return None
    return record


def listing():
    """Every saved chat, newest first, without their message bodies.

    A damaged file is skipped rather than raised: one bad chat should not be
    able to take the whole sidebar down with it.
    """
    out = []
    for path in directory().glob("*.json"):
        try:
            with path.open(encoding="utf-8") as handle:
                record = json.load(handle)
            messages = record.get("messages") or []
            out.append({
                "id": record.get("id") or path.stem,
                "title": record.get("title") or "Untitled",
                "app": record.get("app"),
                "created": record.get("created"),
                "updated": record.get("updated"),
                "turns": turns(messages),
            })
        except (OSError, ValueError) as e:
            log.warning("Skipping unreadable chat %s: %s", path.name, e)

    out.sort(key=lambda c: c.get("updated") or "", reverse=True)
    return out


def delete(chat_id):
    _path(chat_id).unlink(missing_ok=True)
    return True


def created_at(chat_id):
    """The original creation time, so re-saving does not reset it."""
    try:
        with _path(chat_id).open(encoding="utf-8") as handle:
            return json.load(handle).get("created")
    except (OSError, ValueError):
        return None


def prune():
    """Drop the oldest chats once there are more than HISTORY_MAX.

    Unbounded history is fine for a while and then quietly is not: the
    sidebar is a flat list and every entry is read to build it.
    """
    if HISTORY_MAX <= 0:
        return
    saved = listing()
    for chat in saved[HISTORY_MAX:]:
        try:
            delete(chat["id"])
        except (OSError, ValueError) as e:
            log.warning("Could not prune chat %s: %s", chat["id"], e)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
