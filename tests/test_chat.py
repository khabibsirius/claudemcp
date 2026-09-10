import json

import pytest

import chat_tools
from chat_tools import DESTRUCTIVE, FUNCTIONS, TOOLS


class TestToolRegistry:

    def test_every_schema_has_an_implementation(self):
        assert {t["function"]["name"] for t in TOOLS} == set(FUNCTIONS)

    @pytest.mark.parametrize("tool", TOOLS, ids=lambda t: t["function"]["name"])
    def test_schemas_are_well_formed(self, tool):
        function = tool["function"]
        assert tool["type"] == "function"
        assert function["description"].strip()
        parameters = function["parameters"]
        assert parameters["type"] == "object"
        for name in parameters.get("required", []):
            assert name in parameters["properties"], f"required {name} is not declared"

    def test_reloading_is_marked_destructive(self):
        assert "reload_data" in DESTRUCTIVE

    def test_read_only_actions_are_not_gated(self):
        for name in ("query", "data_model", "data_sources", "read_script"):
            assert name not in DESTRUCTIVE


class FakeEngine:
    def __init__(self):
        self.saved = False
        self.calls = []

    def get_script(self):
        return "///$tab Main\r\nLOAD 1 AS x AUTOGENERATE 1;\r\n"

    def set_script(self, script, validate=True):
        self.calls.append(("set_script", script))
        return "previous"

    def reload_data(self):
        self.calls.append(("reload_data",))
        return {"success": True}

    def get_tables(self):
        return [{"name": "Orders", "rows": 100, "fields": []}]

    def save(self):
        self.saved = True


class TestActions:

    def test_read_script_lists_tabs(self):
        assert chat_tools.read_script(FakeEngine())["tabs"] == ["Main"]

    def test_read_script_reports_an_unknown_tab(self):
        assert "error" in chat_tools.read_script(FakeEngine(), tab="nope")

    def test_write_script_targets_one_tab(self):
        engine = FakeEngine()
        result = chat_tools.write_script(engine, content="LOAD 2 AS y AUTOGENERATE 1;", tab="Loader")

        assert result["tabs"] == ["Main", "Loader"]
        written = engine.calls[0][1]
        assert "AS y" in written
        assert "LOAD 1 AS x AUTOGENERATE 1;" in written, "hand-written tab was lost"

    def test_write_script_rejects_an_unknown_mode(self):
        assert "error" in chat_tools.write_script(FakeEngine(), content="x", mode="wat")

    def test_reload_reports_resulting_tables(self):
        assert chat_tools.reload_data(FakeEngine())["tables"] == [
            {"name": "Orders", "rows": 100}
        ]


class ScriptedClient:
    def __init__(self, replies):
        self._replies = list(replies)
        self.requests = []

    def chat(self, model, messages, tools=None, options=None):
        self.requests.append({
            "messages": list(messages), "tools": tools, "options": options,
        })
        return {"message": self._replies.pop(0)}


def tool_call(name, arguments):
    return {"function": {"name": name, "arguments": arguments}}


class TestToolResultMessages:
    def turn(self, monkeypatch, result):
        monkeypatch.setitem(FUNCTIONS, "query", lambda engine, **kw: result)
        client = ScriptedClient([
            {"content": "", "tool_calls": [tool_call("query", {})]},
            {"content": "done", "tool_calls": []},
        ])
        messages = []
        chat_tools.run_agent(client, "m", FakeEngine(), messages)
        return messages[1]

    def test_results_carry_the_tool_name(self, monkeypatch):
        assert self.turn(monkeypatch, {"rows": []})["tool_name"] == "query"

    def test_oversized_results_end_with_a_visible_marker(self, monkeypatch):
        big = {"rows": "x" * (chat_tools.TOOL_RESULT_CHARS * 2)}
        content = self.turn(monkeypatch, big)["content"]

        assert content.endswith("...[truncated]")
        assert len(content) == chat_tools.TOOL_RESULT_CHARS + len("...[truncated]")

    def test_small_results_stay_intact_json(self, monkeypatch):
        content = self.turn(monkeypatch, {"rows": [1]})["content"]
        assert json.loads(content) == {"rows": [1]}


class TestHistoryBudgetSeesToolCalls:
    def test_tool_call_payloads_count_toward_the_budget(self):
        script_call = {
            "function": {"name": "write_script", "arguments": {"content": "x" * 50_000}}
        }
        messages = [
            {"role": "user", "content": "load it"},
            {"role": "assistant", "content": "", "tool_calls": [script_call]},
            {"role": "tool", "content": "ok"},
            {"role": "assistant", "content": "done"},
            {"role": "user", "content": "new question"},
            {"role": "assistant", "content": "answer"},
        ]
        trimmed = chat_tools.trim_history(messages, max_chars=10_000)
        assert trimmed[0]["content"] == "new question", (
            "the turn with the oversized tool_calls payload was kept"
        )

    def test_the_fallback_never_strands_a_tool_result(self):
        messages = [
            {"role": "assistant", "content": "",
             "tool_calls": [{"function": {"name": "query", "arguments": {}}}]},
            {"role": "tool", "content": "x" * 100},
            {"role": "assistant", "content": "y"},
        ]
        trimmed = chat_tools.trim_history(messages, max_chars=10)
        assert trimmed[0]["role"] != "tool"


class TestToolCallsSurviveSaving:
    def test_typed_tool_calls_are_stored_as_plain_dicts(self):
        """A client that hands back objects rather than dicts.

        This used to import ollama's pydantic types. The dependency is gone,
        but the normalising is still worth holding onto: a conversation is
        written to disk as JSON, and an object in there is a turn that cannot
        be reloaded.
        """
        class Function:
            name = "query"
            arguments = {"limit": 5}

        class ToolCall:
            function = Function()

        typed = ToolCall()
        client = ScriptedClient([
            {"content": "", "tool_calls": [typed]},
            {"content": "done", "tool_calls": []},
        ])
        messages = [{"role": "user", "content": "top 5?"}]
        chat_tools.run_agent(client, "m", FakeEngine(), messages)

        recorded = [m for m in messages if m.get("tool_calls")]
        assert recorded, "the tool-call turn was not recorded"
        for message in recorded:
            for call in message["tool_calls"]:
                assert isinstance(call, dict)
        assert json.loads(json.dumps(messages, default=str)) == messages


class TestDeleteSheetNeedsPermission:
    class Sheets:
        def __init__(self):
            self.deleted = []

        def delete_sheet(self, sheet):
            self.deleted.append(sheet)
            return {"deleted": "SH_1", "title": sheet, "charts": 3}

    def test_it_is_declined_without_confirmation(self):
        engine = self.Sheets()

        result = chat_tools.execute(engine, "delete_sheet", {"sheet": "Spare"})

        assert result["cancelled"] is True
        assert engine.deleted == []

    def test_the_question_names_the_sheet(self):
        engine = self.Sheets()
        asked = []

        chat_tools.execute(engine, "delete_sheet", {"sheet": "Spare"},
                           confirm=lambda q, action: asked.append((q, action)) or False)

        question, action = asked[0]
        assert "Spare" in question
        assert action == "delete_sheet"

    def test_it_runs_once_permitted(self):
        engine = self.Sheets()

        result = chat_tools.execute(engine, "delete_sheet", {"sheet": "Spare"},
                                    confirm=lambda q, action: True)

        assert engine.deleted == ["Spare"]
        assert result["charts"] == 3

    def test_the_schema_says_it_cannot_be_undone(self):
        described = next(t["function"]["description"] for t in chat_tools.TOOLS
                         if t["function"]["name"] == "delete_sheet")
        assert "not undoable" in described


class TestBuildDashboardSaysItMakesANewSheet:
    def test_the_schema_says_each_call_is_a_new_sheet(self):
        described = next(t["function"]["description"] for t in chat_tools.TOOLS
                         if t["function"]["name"] == "build_dashboard")
        assert "NEW" in described
        assert "ONE call" in described
