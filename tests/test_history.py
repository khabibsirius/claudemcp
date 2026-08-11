"""Saved conversations: what lands on disk, and what comes back.

The point of this history is that reopening a chat restores the model's
context and not just the transcript, so the tests check the message list
itself rather than only what the sidebar would show.
"""

import json

import pytest

import history
import session
import web_app


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    """Point history at a scratch directory for every test in this file."""
    monkeypatch.setattr(history, "HISTORY_DIR", str(tmp_path))
    monkeypatch.setattr(history, "HISTORY_MAX", 200)
    return tmp_path


def chat(*questions):
    messages = [{"role": "system", "content": "rules"}]
    for question in questions:
        messages.append({"role": "user", "content": question})
        messages.append({"role": "assistant", "content": "answer to " + question})
    return messages


# ----------------------------------------------------------------------
# Ids
# ----------------------------------------------------------------------

def test_new_id_is_valid_and_unique():
    first, second = history.new_id(), history.new_id()
    assert history.valid_id(first)
    assert first != second


@pytest.mark.parametrize("bad", [
    "../../../etc/passwd",
    "..\\..\\windows\\system32",
    "20260810-120000-ab",        # too short
    "20260810-120000-zzzz",      # not hex
    "20260810-120000-abcd.json",
    "", None, "*",
])
def test_bad_ids_are_rejected(bad):
    """Ids come back from the browser, so they are validated on the way in."""
    assert not history.valid_id(bad)
    with pytest.raises(ValueError):
        history._path(bad)


def test_traversal_cannot_escape_the_directory(store):
    """The guard is what stops a crafted id writing outside the store."""
    with pytest.raises(ValueError):
        history.save("../escaped", chat("hi"), "data")
    assert not (store.parent / "escaped.json").exists()


# ----------------------------------------------------------------------
# Round trip
# ----------------------------------------------------------------------

def test_save_then_load_returns_the_same_messages():
    chat_id = history.new_id()
    messages = chat("what is total sales?")

    history.save(chat_id, messages, "data")
    record = history.load(chat_id)

    assert record["messages"] == messages
    assert record["app"] == "data"
    assert record["id"] == chat_id


def test_title_comes_from_the_first_question():
    assert history.title_from(chat("Build me a dashboard")) == "Build me a dashboard"


def test_long_title_is_truncated():
    title = history.title_from(chat("x" * 200))
    assert len(title) == history.TITLE_CHARS + 1 and title.endswith("…")


def test_title_ignores_the_system_prompt():
    messages = [{"role": "system", "content": "You are a Qlik assistant"}]
    assert history.title_from(messages) == "New chat"


def test_empty_conversation_is_not_saved():
    """Opening or switching apps must not litter the sidebar with blanks."""
    chat_id = history.new_id()
    assert history.save(chat_id, [{"role": "system", "content": "rules"}]) is None
    assert history.load(chat_id) is None
    assert history.listing() == []


def test_resaving_keeps_the_original_creation_time():
    chat_id = history.new_id()
    history.save(chat_id, chat("first"), "data")
    created = history.load(chat_id)["created"]

    history.save(chat_id, chat("first", "second"), "data")
    assert history.load(chat_id)["created"] == created


# ----------------------------------------------------------------------
# Listing
# ----------------------------------------------------------------------

def test_listing_is_newest_first():
    for name, stamp in [("old", "2026-01-01T00:00:00+00:00"),
                        ("new", "2026-08-01T00:00:00+00:00")]:
        chat_id = history.new_id()
        history.save(chat_id, chat(name), "data")
        path = history._path(chat_id)
        record = json.loads(path.read_text(encoding="utf-8"))
        record["updated"] = stamp
        path.write_text(json.dumps(record), encoding="utf-8")

    assert [c["title"] for c in history.listing()] == ["new", "old"]


def test_listing_counts_turns_not_messages():
    history.save(history.new_id(), chat("one", "two"), "data")
    assert history.listing()[0]["turns"] == 2


def test_one_damaged_file_does_not_break_the_list(store):
    """A bad chat should cost its own row, not the whole sidebar."""
    history.save(history.new_id(), chat("good"), "data")
    (store / "20260101-000000-dead.json").write_text("{not json", encoding="utf-8")

    listed = history.listing()
    assert [c["title"] for c in listed] == ["good"]


def test_load_returns_none_for_a_damaged_file(store):
    (store / "20260101-000000-dead.json").write_text("{not json", encoding="utf-8")
    assert history.load("20260101-000000-dead") is None


def test_load_rejects_a_file_of_the_wrong_shape(store):
    (store / "20260101-000000-beef.json").write_text('["nope"]', encoding="utf-8")
    assert history.load("20260101-000000-beef") is None


# ----------------------------------------------------------------------
# Pruning and deleting
# ----------------------------------------------------------------------

def test_prune_keeps_only_the_newest(monkeypatch):
    monkeypatch.setattr(history, "HISTORY_MAX", 3)
    for n in range(6):
        chat_id = history.new_id()
        history.save(chat_id, chat(f"q{n}"), "data")
        path = history._path(chat_id)
        record = json.loads(path.read_text(encoding="utf-8"))
        record["updated"] = f"2026-01-0{n + 1}T00:00:00+00:00"
        path.write_text(json.dumps(record), encoding="utf-8")
        history.prune()

    assert [c["title"] for c in history.listing()] == ["q5", "q4", "q3"]


def test_zero_max_keeps_everything(monkeypatch):
    monkeypatch.setattr(history, "HISTORY_MAX", 0)
    for n in range(5):
        history.save(history.new_id(), chat(f"q{n}"), "data")
    assert len(history.listing()) == 5


def test_delete_removes_the_file():
    chat_id = history.new_id()
    history.save(chat_id, chat("bye"), "data")
    history.delete(chat_id)
    assert history.load(chat_id) is None


def test_deleting_a_missing_chat_is_not_an_error():
    history.delete(history.new_id())


# ----------------------------------------------------------------------
# The live session
# ----------------------------------------------------------------------

def test_reset_files_the_old_chat_and_starts_a_new_one():
    session.reset_chat()
    first = session.chat_id()
    session.messages().append({"role": "user", "content": "keep me"})
    session.persist()

    session.reset_chat()
    assert session.chat_id() != first
    assert history.load(first)["messages"][-1]["content"] == "keep me"


def test_reopening_restores_what_the_model_remembers():
    """The whole point: context comes back, not just the pixels."""
    session.reset_chat()
    session.messages().append({"role": "user", "content": "remember this"})
    session.persist()
    saved = session.chat_id()

    session.reset_chat()
    assert "remember this" not in str(session.messages())

    session.load_chat(saved)
    assert session.chat_id() == saved
    assert session.messages()[-1]["content"] == "remember this"


def test_reopening_repairs_a_file_with_no_system_prompt(store):
    """Hand-edited or older files still have to carry the tool rules."""
    chat_id = history.new_id()
    history.save(chat_id, [{"role": "user", "content": "hi"}], "data")

    session.load_chat(chat_id)
    assert session.messages()[0]["role"] == "system"
    assert session.messages()[-1]["content"] == "hi"


def test_reopening_a_missing_chat_leaves_the_session_alone():
    session.reset_chat()
    session.messages().append({"role": "user", "content": "still here"})
    open_id = session.chat_id()

    assert session.load_chat(history.new_id()) is None
    assert session.chat_id() == open_id
    assert session.messages()[-1]["content"] == "still here"


def test_switching_chats_saves_the_one_being_left():
    session.reset_chat()
    session.messages().append({"role": "user", "content": "first chat"})
    session.persist()
    first = session.chat_id()

    session.reset_chat()
    session.messages().append({"role": "user", "content": "second chat"})
    session.persist()
    second = session.chat_id()

    session.load_chat(first)
    assert history.load(second)["messages"][-1]["content"] == "second chat"


def test_deleting_the_open_chat_starts_a_fresh_one():
    session.reset_chat()
    session.messages().append({"role": "user", "content": "doomed"})
    session.persist()
    open_id = session.chat_id()

    session.delete_chat(open_id)
    assert session.chat_id() != open_id
    assert history.load(open_id) is None
    assert len(session.messages()) == 1      # the system prompt only


def test_an_unwritable_store_does_not_lose_the_answer(monkeypatch):
    """History is a convenience; a failure to save must not fail the turn."""
    def boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(history, "save", boom)
    session.reset_chat()
    session.messages().append({"role": "user", "content": "still answered"})

    assert session.persist() is None
    assert session.messages()[-1]["content"] == "still answered"


# ----------------------------------------------------------------------
# What the browser is shown
# ----------------------------------------------------------------------

def test_transcript_hides_the_plumbing():
    """Replaying tool JSON at the user would show them what they never saw."""
    messages = [
        {"role": "system", "content": "the rules"},
        {"role": "user", "content": "chart my sales"},
        {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "x"}}]},
        {"role": "tool", "content": '{"ok": true}'},
        {"role": "assistant", "content": "Done — chart added."},
    ]
    assert web_app.transcript(messages) == [
        {"role": "user", "content": "chart my sales"},
        {"role": "assistant", "content": "Done — chart added."},
    ]


def test_transcript_of_nothing_is_empty():
    assert web_app.transcript(None) == []
    assert web_app.transcript([]) == []


# ----------------------------------------------------------------------
# Over HTTP
# ----------------------------------------------------------------------

@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from tests.test_web_app import FakeEngine

    session._state.update({
        "engine": FakeEngine(), "app_name": "data", "model": "test-model",
        "client": None, "messages": [], "chat_id": None,
    })
    session.reset_chat()
    yield TestClient(web_app.app)
    session._state.update({
        "engine": None, "app_name": None, "client": None,
        "messages": [], "chat_id": None,
    })


def test_state_carries_the_open_chat_back_to_a_refreshed_page(client):
    """The refresh case: the server still has the conversation."""
    session.messages().append({"role": "user", "content": "what is loaded?"})
    session.persist()

    state = client.get("/api/state").json()
    assert state["chat_id"] == session.chat_id()
    assert state["transcript"] == [{"role": "user", "content": "what is loaded?"}]


def test_listing_endpoint_reports_the_open_chat(client):
    session.messages().append({"role": "user", "content": "hello"})
    session.persist()

    body = client.get("/api/chats").json()
    assert body["chat_id"] == session.chat_id()
    assert body["chats"][0]["title"] == "hello"


def test_new_chat_endpoint_files_the_old_one(client):
    session.messages().append({"role": "user", "content": "the first one"})
    session.persist()
    first = session.chat_id()

    body = client.post("/api/chats").json()
    assert body["chat_id"] != first
    assert [c["id"] for c in body["chats"]] == [first]


def test_open_endpoint_restores_context_and_transcript(client):
    session.messages().append({"role": "user", "content": "restore me"})
    session.persist()
    saved = session.chat_id()
    client.post("/api/chats")

    body = client.get(f"/api/chats/{saved}").json()
    assert body["transcript"] == [{"role": "user", "content": "restore me"}]
    assert session.chat_id() == saved
    assert session.messages()[-1]["content"] == "restore me"


def test_delete_endpoint_removes_the_chat(client):
    session.messages().append({"role": "user", "content": "delete me"})
    session.persist()
    doomed = session.chat_id()

    body = client.delete(f"/api/chats/{doomed}").json()
    assert body["chats"] == []
    assert history.load(doomed) is None


@pytest.mark.parametrize("path", [
    "/api/chats/..%2F..%2Fsecrets",
    "/api/chats/not-an-id",
    "/api/chats/20260101-000000-abcd",   # well-formed but absent
])
def test_bad_chat_ids_are_404_not_a_stack_trace(client, path):
    assert client.get(path).status_code == 404
