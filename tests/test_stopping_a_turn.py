import pytest

from chat_tools import STOPPED_NOTE, close_open_tool_calls


def call(name):
    return {"function": {"name": name, "arguments": "{}"}}


def tool_result(name="build_dashboard"):
    return {"role": "tool", "tool_name": name, "content": "{}"}


class TestUnansweredCallsAreClosed:

    def test_a_call_cut_off_before_it_ran_is_answered(self):
        messages = [
            {"role": "user", "content": "build me one"},
            {"role": "assistant", "content": "", "tool_calls": [call("build_dashboard")]},
        ]
        assert close_open_tool_calls(messages) == 1
        assert messages[-1]["role"] == "tool"
        assert STOPPED_NOTE in messages[-1]["content"]

    def test_only_the_unanswered_ones_are_closed(self):
        messages = [
            {"role": "assistant", "content": "",
             "tool_calls": [call("query"), call("build_dashboard")]},
            tool_result("query"),
        ]
        assert close_open_tool_calls(messages) == 1
        assert len(messages) == 3
        assert messages[-1]["tool_name"] == "build_dashboard"

    def test_a_finished_turn_is_left_alone(self):
        messages = [
            {"role": "assistant", "content": "", "tool_calls": [call("query")]},
            tool_result("query"),
        ]
        assert close_open_tool_calls(messages) == 0
        assert len(messages) == 2

    def test_a_turn_with_no_tool_calls_is_left_alone(self):
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "Hello."},
        ]
        assert close_open_tool_calls(messages) == 0
        assert len(messages) == 2

    def test_an_empty_conversation_is_survivable(self):
        messages = []
        assert close_open_tool_calls(messages) == 0

    def test_every_call_is_named_in_its_answer(self):
        messages = [
            {"role": "assistant", "content": "",
             "tool_calls": [call("query"), call("create_chart")]},
        ]
        close_open_tool_calls(messages)
        assert [m["tool_name"] for m in messages[1:]] == ["query", "create_chart"]


class TestStoppingTheGenerator:
    def test_closing_mid_turn_closes_the_open_calls(self):
        from chat_tools import stream_agent

        messages = [{"role": "user", "content": "build me five charts"}]

        class Client:
            def chat(self, **kwargs):
                yield {"message": {
                    "content": "",
                    "tool_calls": [{"function": {
                        "name": "list_charts", "arguments": "{}",
                    }}],
                }}

        events = stream_agent(Client(), "m", None, messages, allowed=set())
        first = next(events)
        assert first["type"] == "tool"

        events.close()

        assert messages[-1]["role"] == "tool"
        assert STOPPED_NOTE in messages[-1]["content"]

    def test_a_turn_that_runs_to_the_end_needs_no_repair(self):
        from chat_tools import stream_agent

        messages = [{"role": "user", "content": "hello"}]

        class Client:
            def chat(self, **kwargs):
                yield {"message": {"content": "Hello.", "tool_calls": []}}

        events = list(stream_agent(Client(), "m", None, messages))
        assert events[-1]["type"] == "done"
        assert not [m for m in messages if m.get("role") == "tool"]
