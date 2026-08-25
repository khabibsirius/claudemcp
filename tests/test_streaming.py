"""Streamed chat.

A local model can spend minutes on a multi-step request. Returning only the
finished answer makes that indistinguishable from a hang, so each tool call
and each token is emitted as it happens.
"""

import json

import pytest

import session
import web_app
from chat_tools import call_parts, stream_agent


class Function:
    """Mimics ollama's tool-call object, which is not a dict."""

    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class ToolCall:
    def __init__(self, name, arguments):
        self.function = Function(name, arguments)


class StreamingClient:
    """Replays scripted turns as ollama would stream them."""

    def __init__(self, turns):
        self._turns = list(turns)
        self.calls = 0

    def chat(self, model, messages, tools=None, stream=False, options=None):
        self.calls += 1
        assert stream is True, "stream_agent must ask for a stream"
        return iter(self._turns.pop(0))


def text_chunks(*pieces):
    return [{"message": {"content": p}} for p in pieces]


def tool_chunk(name, arguments=None):
    return [{"message": {"content": "", "tool_calls": [ToolCall(name, arguments or {})]}}]


class FakeEngine:
    connected = True
    mode = "desktop"

    def get_script(self):
        return "///$tab Main\r\nLOAD 1;\r\n"

    def list_sheets(self):
        return []

    def app_file_info(self):
        return {"path": "a.qvf", "size_bytes": 1, "modified": "2026-08-03 14:33:31"}


class TestCallParts:

    def test_reads_an_ollama_object(self):
        assert call_parts(ToolCall("query", {"limit": 5})) == ("query", {"limit": 5})

    def test_reads_a_plain_dict(self):
        call = {"function": {"name": "query", "arguments": {"limit": 5}}}
        assert call_parts(call) == ("query", {"limit": 5})

    def test_parses_stringified_arguments(self):
        call = {"function": {"name": "query", "arguments": '{"limit": 3}'}}
        assert call_parts(call)[1] == {"limit": 3}

    def test_unparseable_arguments_become_empty(self):
        call = {"function": {"name": "query", "arguments": "not json"}}
        assert call_parts(call)[1] == {}

    def test_missing_arguments_become_empty(self):
        assert call_parts({"function": {"name": "save"}}) == ("save", {})


class TestStreamAgent:

    def test_emits_tokens_as_they_arrive(self):
        client = StreamingClient([text_chunks("Total ", "is ", "5.")])
        events = list(stream_agent(client, "m", FakeEngine(), []))

        tokens = [e["text"] for e in events if e["type"] == "token"]
        assert tokens == ["Total ", "is ", "5."]

    def test_finishes_with_the_assembled_reply(self):
        client = StreamingClient([text_chunks("Total ", "is ", "5.")])
        events = list(stream_agent(client, "m", FakeEngine(), []))
        assert events[-1] == {"type": "done", "reply": "Total is 5."}

    def test_announces_a_tool_before_running_it(self, monkeypatch):
        from chat_tools import FUNCTIONS

        order = []
        monkeypatch.setitem(
            FUNCTIONS, "query",
            lambda engine, **kw: order.append("ran") or {"rows": []},
        )

        client = StreamingClient([tool_chunk("query", {"limit": 5}), text_chunks("Done.")])
        events = list(stream_agent(client, "m", FakeEngine(), []))

        kinds = [e["type"] for e in events]
        assert kinds.index("tool") < kinds.index("tool_result")
        assert order == ["ran"]

    def test_tool_event_carries_the_arguments(self, monkeypatch):
        from chat_tools import FUNCTIONS

        monkeypatch.setitem(FUNCTIONS, "query", lambda engine, **kw: {"rows": []})
        client = StreamingClient([tool_chunk("query", {"limit": 5}), text_chunks("ok")])

        tool = next(e for e in stream_agent(client, "m", FakeEngine(), []) if e["type"] == "tool")
        assert tool == {"type": "tool", "name": "query", "arguments": {"limit": 5}}

    def test_a_failing_tool_is_reported_not_raised(self, monkeypatch):
        from chat_tools import FUNCTIONS

        def boom(engine, **kwargs):
            raise ValueError("no such field")

        monkeypatch.setitem(FUNCTIONS, "query", boom)
        client = StreamingClient([tool_chunk("query"), text_chunks("Sorry.")])

        events = list(stream_agent(client, "m", FakeEngine(), []))
        result = next(e for e in events if e["type"] == "tool_result")

        assert result["ok"] is False
        assert "no such field" in result["detail"]

    def test_the_conversation_is_extended_for_the_next_turn(self, monkeypatch):
        from chat_tools import FUNCTIONS

        monkeypatch.setitem(FUNCTIONS, "query", lambda engine, **kw: {"rows": [1]})
        messages = []
        client = StreamingClient([tool_chunk("query"), text_chunks("Done.")])

        list(stream_agent(client, "m", FakeEngine(), messages))

        assert [m["role"] for m in messages] == ["assistant", "tool", "assistant"]
        assert json.loads(messages[1]["content"]) == {"rows": [1]}

    def test_withheld_tools_are_not_offered(self, monkeypatch):
        seen = {}

        class Recorder(StreamingClient):
            def chat(self, model, messages, tools=None, stream=False, options=None):
                seen["tools"] = [t["function"]["name"] for t in tools]
                return super().chat(model, messages, tools, stream, options)

        client = Recorder([text_chunks("hi")])
        list(stream_agent(client, "m", FakeEngine(), [], allowed={"query"}))

        assert seen["tools"] == ["query"]

    def test_it_stops_after_the_step_limit(self, monkeypatch):
        from chat_tools import FUNCTIONS

        monkeypatch.setitem(FUNCTIONS, "query", lambda engine, **kw: {"ok": True})
        client = StreamingClient([tool_chunk("query")] * 6)

        events = list(stream_agent(client, "m", FakeEngine(), [], max_steps=3))

        assert client.calls == 3
        assert "too many steps" in events[-1]["reply"]


class TestStreamEndpoint:

    @pytest.fixture
    def wired(self, monkeypatch):
        fake = FakeEngine()
        session._state.update({
            "engine": fake, "app_name": "data", "model": "m",
            "client": None, "messages": [], "allow_reload": True,
        })
        yield fake
        session._state.update({"engine": None, "app_name": None, "messages": []})

    def frames(self, response):
        return [
            json.loads(line[6:])
            for line in response.text.split("\n\n")
            if line.startswith("data: ")
        ]

    def test_streams_sse_frames(self, wired, monkeypatch):
        from fastapi.testclient import TestClient

        monkeypatch.setattr(
            web_app, "stream_agent",
            lambda *a, **kw: iter([
                {"type": "tool", "name": "query", "arguments": {}},
                {"type": "token", "text": "Five."},
                {"type": "done", "reply": "Five."},
            ]),
        )

        response = TestClient(web_app.app).post(
            "/api/chat/stream", json={"message": "how many?"}
        )

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        kinds = [f["type"] for f in self.frames(response)]
        assert kinds == ["tool", "token", "done", "final"]

    def test_the_final_frame_carries_the_page_state(self, wired, monkeypatch):
        from fastapi.testclient import TestClient

        monkeypatch.setattr(
            web_app, "stream_agent", lambda *a, **kw: iter([{"type": "done", "reply": "ok"}])
        )
        response = TestClient(web_app.app).post("/api/chat/stream", json={"message": "hi"})

        final = self.frames(response)[-1]
        assert final["type"] == "final"
        assert "script" in final and "sheets" in final and "file" in final

    def test_a_crash_becomes_an_error_frame(self, wired, monkeypatch):
        from fastapi.testclient import TestClient

        def boom(*args, **kwargs):
            raise RuntimeError("model died")

        monkeypatch.setattr(web_app, "stream_agent", boom)
        response = TestClient(web_app.app).post("/api/chat/stream", json={"message": "hi"})

        frames = self.frames(response)
        assert frames[-1]["type"] == "error"
        assert "model died" in frames[-1]["message"]


class TestMalformedStreamArguments:
    """Broken JSON in a streamed tool call used to become {} silently, and
    the tool then ran with every default - write_script wrote an empty tab
    over the script the model had just composed."""

    def test_the_tool_is_refused_with_the_reason(self, monkeypatch):
        from chat_tools import FUNCTIONS

        ran = []
        monkeypatch.setitem(
            FUNCTIONS, "query", lambda engine, **kw: ran.append(kw) or {"ok": True}
        )

        messages = []
        client = StreamingClient([
            tool_chunk("query", '{"limit": bro'), text_chunks("Sorry."),
        ])
        events = list(stream_agent(client, "m", FakeEngine(), messages))

        assert ran == [], "the tool ran despite unparseable arguments"
        result = next(e for e in events if e["type"] == "tool_result")
        assert result["ok"] is False
        assert "not valid JSON" in result["detail"]

    def test_results_carry_the_tool_name(self, monkeypatch):
        """Without tool_name on the role:'tool' message, a turn with two
        calls leaves the model matching results to calls by position."""
        from chat_tools import FUNCTIONS

        monkeypatch.setitem(FUNCTIONS, "query", lambda engine, **kw: {"rows": []})
        messages = []
        client = StreamingClient([tool_chunk("query"), text_chunks("Done.")])
        list(stream_agent(client, "m", FakeEngine(), messages))

        assert messages[1]["tool_name"] == "query"
