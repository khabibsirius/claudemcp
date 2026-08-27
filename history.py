import json
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import HISTORY_DIR, HISTORY_MAX

log = logging.getLogger(__name__)

ID_PATTERN = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{4}\Z")

OWNER_PATTERN = re.compile(r"^u[0-9]{1,12}\Z")

TITLE_CHARS = 60


def owner_key(user_id):
    return f"u{int(user_id)}"


def valid_owner(owner):
    return bool(owner) and bool(OWNER_PATTERN.match(str(owner)))


def directory(owner=None):
    path = Path(HISTORY_DIR).expanduser()
    if owner is not None:
        if not valid_owner(owner):
            raise ValueError(f"Not an owner key: {owner!r}")
        path = path / str(owner)
    path.mkdir(parents=True, exist_ok=True)
    return path


def owners():
    root = Path(HISTORY_DIR).expanduser()
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir()
                  if p.is_dir() and valid_owner(p.name))


def new_id():
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{os.urandom(2).hex()}"


def valid_id(chat_id):
    return bool(chat_id) and bool(ID_PATTERN.match(str(chat_id)))


def _path(chat_id, owner=None):
    if not valid_id(chat_id):
        raise ValueError(f"Not a chat id: {chat_id!r}")
    return directory(owner) / f"{chat_id}.json"


def title_from(messages):
    for message in messages:
        if message.get("role") == "user":
            text = " ".join((message.get("content") or "").split())
            if text:
                return text[:TITLE_CHARS] + ("…" if len(text) > TITLE_CHARS else "")
    return "New chat"


def turns(messages):
    return sum(1 for m in messages if m.get("role") == "user")


def save(chat_id, messages, app_name=None, title=None, owner=None):
    if not turns(messages):
        return None

    previous = load(chat_id, owner)
    if previous is not None and previous.get("messages") == messages:
        return previous

    named_by_hand = bool((previous or {}).get("renamed"))
    if title:
        name = clean_title(title)
    elif named_by_hand:
        name = previous.get("title") or title_from(messages)
    else:
        name = title_from(messages)

    record = {
        "id": chat_id,
        "title": name,
        "renamed": named_by_hand,
        "app": app_name,
        "owner": owner,
        "created": (previous or {}).get("created") or created_at(chat_id, owner) or _now(),
        "updated": _now(),
        "messages": messages,
    }

    _write(chat_id, record, owner)
    prune(owner)
    return record


def _write(chat_id, record, owner=None):
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
    text = " ".join(str(title or "").split())
    return text[:TITLE_CHARS] + ("…" if len(text) > TITLE_CHARS else "")


def rename(chat_id, title, owner=None):
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
    try:
        with _path(chat_id, owner).open(encoding="utf-8") as handle:
            record = json.load(handle)
    except FileNotFoundError:
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
    out = []
    for owner in owners():
        out.extend(listing(owner))
    out.sort(key=lambda c: c.get("updated") or "", reverse=True)
    return out


def delete(chat_id, owner=None):
    _path(chat_id, owner).unlink(missing_ok=True)
    return True


def created_at(chat_id, owner=None):
    try:
        with _path(chat_id, owner).open(encoding="utf-8") as handle:
            return json.load(handle).get("created")
    except (OSError, ValueError):
        return None


def prune(owner=None):
    if HISTORY_MAX <= 0:
        return
    saved = listing(owner)
    for chat in saved[HISTORY_MAX:]:
        try:
            delete(chat["id"], owner)
        except (OSError, ValueError) as e:
            log.warning("Could not prune chat %s: %s", chat["id"], e)


def prune_old(days=None, dry_run=False):
    from config import HISTORY_RETENTION_DAYS

    days = HISTORY_RETENTION_DAYS if days is None else days
    if days <= 0:
        return 0

    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=days)).isoformat(timespec="seconds")
    removed = 0
    for owner in [None] + owners():
        for chat in listing(owner):
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
