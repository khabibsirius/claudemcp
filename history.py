"""Saved conversations, one JSON file each, in a folder per person.

The assistant's memory is the message list in session.py, and until now it
lived only in the server process: refreshing the browser lost the transcript,
and stopping the server lost the conversation. This module writes that same
list to disk after every turn so a chat can be reopened later with the model's
context intact - reloading the pixels without the messages would give the
assistant amnesia while the screen showed a full conversation.

Files rather than a database: a conversation is only ever read or written
whole, there is exactly one writer, and a directory of readable JSON is
something the user can back up, diff or delete by hand. (Accounts are the
opposite on every count, which is why users.py is SQLite instead.)

Each person's chats live in a directory of their own, named after their user
id. That is what keeps one user's conversation out of another's sidebar, and
it is a directory rather than an `owner` field inside each file because the
sidebar is built by reading every file in it - filtering by owner would mean
reading everyone's conversations to show one person theirs.
"""

import json
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import HISTORY_DIR, HISTORY_MAX

log = logging.getLogger(__name__)

# Ids are built here and then handed back to us by the browser, so they are
# validated on the way in as well: anything outside this alphabet could walk
# out of the history directory when joined to a path. \Z rather than $,
# because $ also matches just before a trailing newline - an id ending in
# a newline would have sailed through to the filesystem.
ID_PATTERN = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{4}\Z")

# An owner is a directory name built from a user id, and it reaches this
# module from a URL on the admin pages. Same reasoning as the ids above,
# and the same \Z.
OWNER_PATTERN = re.compile(r"^u[0-9]{1,12}\Z")

TITLE_CHARS = 60


def owner_key(user_id):
    """The directory name for a user id.

    Deliberately not the username: usernames are shown to people and could
    one day be editable, while a directory full of someone's conversations
    must not move when their name is corrected.
    """
    return f"u{int(user_id)}"


def valid_owner(owner):
    return bool(owner) and bool(OWNER_PATTERN.match(str(owner)))


def directory(owner=None):
    """A person's history directory, created on first use.

    No owner means the root itself, which is what the terminal chat and the
    single-operator install use - there is only one person there, and asking
    them to have an id would be ceremony for its own sake.
    """
    path = Path(HISTORY_DIR).expanduser()
    if owner is not None:
        if not valid_owner(owner):
            raise ValueError(f"Not an owner key: {owner!r}")
        path = path / str(owner)
    path.mkdir(parents=True, exist_ok=True)
    return path


def owners():
    """Every person who has saved a conversation, for the admin pages."""
    root = Path(HISTORY_DIR).expanduser()
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir()
                  if p.is_dir() and valid_owner(p.name))


def new_id():
    """A sortable id: the filename alone puts the newest chat last."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{os.urandom(2).hex()}"


def valid_id(chat_id):
    return bool(chat_id) and bool(ID_PATTERN.match(str(chat_id)))


def _path(chat_id, owner=None):
    if not valid_id(chat_id):
        raise ValueError(f"Not a chat id: {chat_id!r}")
    return directory(owner) / f"{chat_id}.json"


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


def save(chat_id, messages, app_name=None, title=None, owner=None):
    """Write a conversation, replacing any previous version of it.

    Written to a temporary file and renamed, because the alternative - opening
    the real file for writing - truncates it first, so a crash mid-write would
    leave an empty file where the conversation used to be.
    """
    if not turns(messages):
        # Nothing was ever asked. Saving would litter the sidebar with empty
        # chats every time the app is opened or switched.
        return None

    previous = load(chat_id, owner)
    if previous is not None and previous.get("messages") == messages:
        # Leaving a chat writes it again, so a chat that was merely visited
        # used to be re-dated and jump above chats that had actually been
        # talked in - the sidebar is ordered by "updated", and it reshuffled
        # as the user browsed it. "updated" is when the conversation last
        # changed, not when it was last written.
        return previous

    named_by_hand = bool((previous or {}).get("renamed"))
    if title:
        name = clean_title(title)
    elif named_by_hand:
        # A name the person chose outlives the question the chat started
        # from - otherwise the next answer would rename it back.
        name = previous.get("title") or title_from(messages)
    else:
        name = title_from(messages)

    record = {
        "id": chat_id,
        "title": name,
        "renamed": named_by_hand,
        "app": app_name,
        # Written into the file as well as being the folder it sits in, so a
        # conversation moved or restored by hand still says whose it is.
        "owner": owner,
        "created": (previous or {}).get("created") or created_at(chat_id, owner) or _now(),
        "updated": _now(),
        "messages": messages,
    }

    _write(chat_id, record, owner)
    prune(owner)
    return record


def _write(chat_id, record, owner=None):
    """Put a record on disk without risking the one already there.

    Written to a temporary file and renamed, because the alternative - opening
    the real file for writing - truncates it first, so a crash mid-write would
    leave an empty file where the conversation used to be.
    """
    path = _path(chat_id, owner)
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(record, out, ensure_ascii=False, indent=1, default=str)
        os.replace(temporary, path)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise


def clean_title(title):
    """One line, no runs of whitespace, short enough for the sidebar."""
    text = " ".join(str(title or "").split())
    return text[:TITLE_CHARS] + ("…" if len(text) > TITLE_CHARS else "")


def rename(chat_id, title, owner=None):
    """Give a chat a name of its own, or None if there is no such chat.

    A chat is named after the first thing asked of it, which is rarely what
    it turned out to be about. A name chosen here sticks: save() stops
    deriving one once `renamed` is set.

    The name is not a change to the conversation, so "updated" is left
    alone - renaming a chat should not move it around the sidebar.
    """
    name = clean_title(title)
    if not name:
        raise ValueError("A chat needs a name.")

    record = load(chat_id, owner)
    if record is None:
        return None

    record["title"] = name
    record["renamed"] = True
    _write(chat_id, record, owner)
    return record


def load(chat_id, owner=None):
    """One conversation, or None if it is missing or unreadable."""
    try:
        with _path(chat_id, owner).open(encoding="utf-8") as handle:
            record = json.load(handle)
    except FileNotFoundError:
        # Not an error: save() reads the previous version of a chat that may
        # never have been written, and the browser can ask for one that has
        # since been deleted. Only a file we cannot make sense of is news.
        return None
    except (OSError, ValueError) as e:
        log.warning("Could not read chat %s: %s", chat_id, e)
        return None

    if not isinstance(record, dict) or not isinstance(record.get("messages"), list):
        log.warning("Chat %s is not in the expected shape", chat_id)
        return None
    record["messages"] = _sane_messages(record["messages"])
    return record


def _sane_messages(messages):
    """Strip artifacts older saves left in a conversation.

    Chats saved before tool calls were normalised to plain dicts hold them
    as repr STRINGS (json.dump's default=str flattened the client's typed
    objects). Sent back to the model they fail its message validation, so
    reopening such a chat crashed on the next question. The calls themselves
    are unrecoverable, but the tool RESULTS that follow still say what
    happened - dropping the broken entries keeps the conversation usable.
    """
    sane = []
    for message in messages:
        if isinstance(message, dict) and message.get("tool_calls"):
            calls = [
                c for c in message["tool_calls"]
                if isinstance(c, dict) and isinstance(c.get("function"), dict)
            ]
            message = {k: v for k, v in message.items() if k != "tool_calls"}
            if calls:
                message["tool_calls"] = calls
        sane.append(message)
    return sane


def listing(owner=None):
    """One person's saved chats, newest first, without their message bodies.

    A damaged file is skipped rather than raised: one bad chat should not be
    able to take the whole sidebar down with it.
    """
    out = []
    for path in directory(owner).glob("*.json"):
        try:
            with path.open(encoding="utf-8") as handle:
                record = json.load(handle)
            messages = record.get("messages") or []
            out.append({
                "id": record.get("id") or path.stem,
                "title": record.get("title") or "Untitled",
                "app": record.get("app"),
                "owner": owner,
                "created": record.get("created"),
                "updated": record.get("updated"),
                "turns": turns(messages),
            })
        except (OSError, ValueError) as e:
            log.warning("Skipping unreadable chat %s: %s", path.name, e)

    out.sort(key=lambda c: c.get("updated") or "", reverse=True)
    return out


def all_listing():
    """Every saved chat by everyone, newest first - the admin's view.

    Reads every conversation on the machine, which is exactly why it is not
    what the sidebar calls: it is for the one page that is meant to see
    across people.
    """
    out = []
    for owner in owners():
        out.extend(listing(owner))
    out.sort(key=lambda c: c.get("updated") or "", reverse=True)
    return out


def delete(chat_id, owner=None):
    _path(chat_id, owner).unlink(missing_ok=True)
    return True


def created_at(chat_id, owner=None):
    """The original creation time, so re-saving does not reset it."""
    try:
        with _path(chat_id, owner).open(encoding="utf-8") as handle:
            return json.load(handle).get("created")
    except (OSError, ValueError):
        return None


def prune(owner=None):
    """Drop a person's oldest chats once there are more than HISTORY_MAX.

    Unbounded history is fine for a while and then quietly is not: the
    sidebar is a flat list and every entry is read to build it. The cap is
    per person, not per machine - one heavy user should not evict everyone
    else's conversations.
    """
    if HISTORY_MAX <= 0:
        return
    saved = listing(owner)
    for chat in saved[HISTORY_MAX:]:
        try:
            delete(chat["id"], owner)
        except (OSError, ValueError) as e:
            log.warning("Could not prune chat %s: %s", chat["id"], e)


def prune_old(days=None, dry_run=False):
    """Delete conversations last touched longer ago than the policy allows.

    By age rather than by count, which is what a retention policy is about:
    HISTORY_MAX stops one person filling the disk, this is the institution
    saying how long a record of what was asked may be kept.
    """
    from config import HISTORY_RETENTION_DAYS

    days = HISTORY_RETENTION_DAYS if days is None else days
    if days <= 0:
        return 0

    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=days)).isoformat(timespec="seconds")
    removed = 0
    for owner in [None] + owners():
        for chat in listing(owner):
            # No timestamp at all is treated as old: it was written by a
            # version that did not record one, so it is older than anything
            # that does.
            if (chat.get("updated") or "") < cutoff:
                if not dry_run:
                    try:
                        delete(chat["id"], owner)
                    except (OSError, ValueError) as e:
                        log.warning("Could not delete chat %s: %s", chat["id"], e)
                        continue
                removed += 1

    if removed and not dry_run:
        log.info("Removed %d conversation(s) last used before %s", removed, cutoff)
    return removed


def adopt_loose_chats(owner):
    """Move conversations saved before there were accounts into a folder.

    They were written straight into the root by the single-operator version.
    Left there they are invisible - every listing now reads a per-person
    directory - so on the first run with accounts they are given to the
    first administrator rather than quietly disappearing.
    """
    root = Path(HISTORY_DIR).expanduser()
    if not root.is_dir():
        return 0

    loose = [p for p in root.glob("*.json") if p.is_file()]
    if not loose:
        return 0

    destination = directory(owner)
    moved = 0
    for path in loose:
        try:
            os.replace(path, destination / path.name)
            moved += 1
        except OSError as e:
            log.warning("Could not move chat %s: %s", path.name, e)

    if moved:
        log.info("Moved %d conversation(s) saved before login into %s", moved, owner)
    return moved


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
