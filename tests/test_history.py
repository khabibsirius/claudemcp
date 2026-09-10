import json

import pytest

import history
import session
import web_app


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "HISTORY_DIR", str(tmp_path))
    monkeypatch.setattr(history, "HISTORY_MAX", 200)
    return tmp_path


def chat(*questions):
    messages = [{"role": "system", "content": "rules"}]
    for question in questions:
        messages.append({"role": "user", "content": question})
        messages.append({"role": "assistant", "content": "answer to " + question})
    return messages


def test_new_id_is_valid_and_unique():
    first, second = history.new_id(), history.new_id()
    assert history.valid_id(first)
    assert first != second


@pytest.mark.parametrize("bad", [
    "../../../etc/passwd",
    "..\\..\\windows\\system32",
    "20260810-120000-ab",
    "20260810-120000-zzzz",
    "20260810-120000-abcd.json",
    "20260810-120000-abcd\n",
    "", None, "*",
])
def test_bad_ids_are_rejected(bad):
    assert not history.valid_id(bad)
    with pytest.raises(ValueError):
        history._path(bad)


def test_traversal_cannot_escape_the_directory(store):
    with pytest.raises(ValueError):
        history.save("../escaped", chat("hi"), "data")
    assert not (store.parent / "escaped.json").exists()


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


def test_reset_files_the_old_chat_and_starts_a_new_one():
    session.reset_chat()
    first = session.chat_id()
    session.messages().append({"role": "user", "content": "keep me"})
    session.persist()

    session.reset_chat()
    assert session.chat_id() != first
    assert history.load(first)["messages"][-1]["content"] == "keep me"


def test_reopening_restores_what_the_model_remembers():
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


def test_reopening_a_chat_reopens_the_app_it_was_about(monkeypatch):
    class Switchable:
        connected = True

        def __init__(self):
            self.opened = []

        def open_app(self, name):
            self.opened.append(name)

        def close(self):
            self.connected = False

    engine = Switchable()
    def new_session():
        engine.connected = True
        return engine

    monkeypatch.setattr(session, "QlikEngine", new_session)
    session._state.update({"engine": engine, "app_name": "sales"})
    try:
        chat_id = history.new_id()
        history.save(chat_id, chat("about finance"), "finance")

        record = session.load_chat(chat_id)

        assert engine.opened == ["finance"]
        assert session.app_name() == "finance"
        assert "app_mismatch" not in record
        assert session.messages()[-1]["content"] == "answer to about finance"
    finally:
        session._state.update({"engine": None, "app_name": None})


def test_a_chat_whose_app_cannot_be_reopened_still_loads_but_says_so():
    class Refuses:
        connected = True

        def open_app(self, name):
            raise RuntimeError("engine says no")

        def close(self):
            self.connected = False

    session._state.update({"engine": Refuses(), "app_name": "sales"})
    try:
        chat_id = history.new_id()
        history.save(chat_id, chat("hello"), "finance")

        record = session.load_chat(chat_id)

        assert "finance" in record["app_mismatch"]
        assert session.chat_id() == chat_id
        assert session.messages()[-1]["content"] == "answer to hello"
    finally:
        session._state.update({"engine": None, "app_name": None})


def test_deleting_the_open_chat_starts_a_fresh_one():
    session.reset_chat()
    session.messages().append({"role": "user", "content": "doomed"})
    session.persist()
    open_id = session.chat_id()

    session.delete_chat(open_id)
    assert session.chat_id() != open_id
    assert history.load(open_id) is None
    assert len(session.messages()) == 1


def test_an_unwritable_store_does_not_lose_the_answer(monkeypatch):
    def boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(history, "save", boom)
    session.reset_chat()
    session.messages().append({"role": "user", "content": "still answered"})

    assert session.persist() is None
    assert session.messages()[-1]["content"] == "still answered"


def test_transcript_hides_the_plumbing():
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

    body = client.post(f"/api/chats/{saved}/open").json()
    assert body["transcript"] == [{"role": "user", "content": "restore me"}]
    assert session.chat_id() == saved
    assert session.messages()[-1]["content"] == "restore me"


def test_get_chat_is_read_only_so_prefetch_cannot_switch(client):
    session.messages().append({"role": "user", "content": "restore me"})
    session.persist()
    saved = session.chat_id()
    client.post("/api/chats")
    fresh = session.chat_id()

    body = client.get(f"/api/chats/{saved}").json()
    assert body["transcript"] == [{"role": "user", "content": "restore me"}]
    assert session.chat_id() == fresh
    assert "restore me" not in str(session.messages())


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
    "/api/chats/20260101-000000-abcd",
])
def test_bad_chat_ids_are_404_not_a_stack_trace(client, path):
    assert client.get(path).status_code == 404
    assert client.post(path + "/open").status_code == 404


def test_a_chat_saved_with_flattened_tool_calls_is_repaired(store):
    chat_id = history.new_id()
    messages = chat("show sales")
    messages.insert(2, {
        "role": "assistant", "content": "",
        "tool_calls": ["function=Function(name='query', arguments={})"],
    })
    messages.insert(3, {"role": "tool", "content": '{"rows": []}'})
    history.save(chat_id, messages)

    restored = history.load(chat_id)["messages"]

    broken = [m for m in restored if m.get("tool_calls") is not None]
    assert broken == [], "string tool calls must not reach the model"
    assert any(m.get("role") == "tool" for m in restored)


def test_repair_keeps_the_tool_calls_that_are_still_dicts(store):
    chat_id = history.new_id()
    messages = chat("show sales")
    good = {"function": {"name": "query", "arguments": {"limit": 5}}}
    messages.insert(2, {
        "role": "assistant", "content": "",
        "tool_calls": ["function=Function(name='save', arguments={})", good],
    })
    history.save(chat_id, messages)

    restored = history.load(chat_id)["messages"]

    kept = [c for m in restored for c in m.get("tool_calls", [])]
    assert kept == [good]


@pytest.fixture
def ticking_clock(monkeypatch):
    tick = iter(f"2026-08-24T10:{minute:02d}:00+00:00" for minute in range(60))
    monkeypatch.setattr(history, "_now", lambda: next(tick))


def test_visiting_a_chat_does_not_move_it_up_the_list(store, ticking_clock):
    older, newer = history.new_id(), history.new_id()
    history.save(older, chat("an old question"))
    history.save(newer, chat("what i just asked"))
    stamps = {c["id"]: c["updated"] for c in history.listing()}

    history.save(older, chat("an old question"))

    assert [c["id"] for c in history.listing()] == [newer, older]
    assert {c["id"]: c["updated"] for c in history.listing()} == stamps


def test_a_chat_that_was_talked_in_does_move_up(store, ticking_clock):
    older, newer = history.new_id(), history.new_id()
    history.save(older, chat("an old question"))
    history.save(newer, chat("what i just asked"))

    history.save(older, chat("an old question", "and one more thing"))

    assert [c["id"] for c in history.listing()] == [older, newer]


def test_rename_gives_the_chat_its_own_name(store):
    chat_id = history.new_id()
    history.save(chat_id, chat("what is total sales?"), "data")

    history.rename(chat_id, "Sales review")

    assert history.load(chat_id)["title"] == "Sales review"
    assert history.listing()[0]["title"] == "Sales review"


def test_a_renamed_chat_keeps_its_name_when_it_is_talked_in_again(store):
    chat_id = history.new_id()
    history.save(chat_id, chat("what is total sales?"), "data")
    history.rename(chat_id, "Sales review")

    history.save(chat_id, chat("what is total sales?", "and by region?"), "data")

    assert history.load(chat_id)["title"] == "Sales review"


def test_renaming_does_not_move_the_chat_up_the_list(store, ticking_clock):
    older, newer = history.new_id(), history.new_id()
    history.save(older, chat("an old question"))
    history.save(newer, chat("what i just asked"))

    history.rename(older, "Renamed")

    assert [c["id"] for c in history.listing()] == [newer, older]


def test_a_long_name_is_cut_like_a_derived_one(store):
    chat_id = history.new_id()
    history.save(chat_id, chat("hi"), "data")

    history.rename(chat_id, "x" * 200)

    title = history.load(chat_id)["title"]
    assert len(title) == history.TITLE_CHARS + 1 and title.endswith("…")


def test_a_name_of_only_whitespace_is_refused(store):
    chat_id = history.new_id()
    history.save(chat_id, chat("hi"), "data")

    with pytest.raises(ValueError):
        history.rename(chat_id, "   ")

    assert history.load(chat_id)["title"] == "hi"


def test_renaming_a_chat_that_is_not_there_is_none(store):
    assert history.rename(history.new_id(), "Nowhere") is None


def test_renaming_keeps_the_conversation_intact(store):
    chat_id = history.new_id()
    messages = chat("what is total sales?", "and by region?")
    history.save(chat_id, messages, "data")

    history.rename(chat_id, "Sales review")

    record = history.load(chat_id)
    assert record["messages"] == messages
    assert record["app"] == "data"
