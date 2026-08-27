import json

import pytest

from ollama_client import (
    OllamaClient,
    OllamaError,
    extract_json_object,
    parse_json_reply,
    strip_code_fences,
)


class TestStripCodeFences:

    def test_unwraps_a_json_fence(self):
        assert strip_code_fences('```json\n{"a": 1}\n```') == '{"a": 1}'

    def test_unwraps_a_bare_fence(self):
        assert strip_code_fences('```\n{"a": 1}\n```') == '{"a": 1}'

    def test_leaves_unfenced_json_alone(self):
        assert strip_code_fences('{"a": 1}') == '{"a": 1}'

    def test_does_not_corrupt_backticks_inside_values(self):
        payload = '{"title": "Sales ```highlight``` report"}'
        assert json.loads(strip_code_fences(payload))["title"] == "Sales ```highlight``` report"


class TestExtractJsonObject:

    def test_pulls_an_object_out_of_prose(self):
        text = 'Sure! Here is your dashboard:\n{"dashboard_title": "Sales"}\nHope that helps!'
        assert json.loads(extract_json_object(text))["dashboard_title"] == "Sales"

    def test_handles_nested_objects(self):
        text = 'note {"a": {"b": {"c": 1}}} end'
        assert json.loads(extract_json_object(text)) == {"a": {"b": {"c": 1}}}

    def test_braces_inside_strings_do_not_end_the_object(self):
        text = '{"title": "Revenue {gross}", "n": 1}'
        assert json.loads(extract_json_object(text)) == {"title": "Revenue {gross}", "n": 1}

    def test_escaped_quotes_do_not_end_the_string(self):
        text = r'{"title": "a \" brace } here", "n": 1}'
        assert json.loads(extract_json_object(text))["n"] == 1

    @pytest.mark.parametrize("text", ["no braces at all", '{"unbalanced": 1'])
    def test_returns_none_when_there_is_no_complete_object(self, text):
        assert extract_json_object(text) is None


class TestParseJsonReply:

    def test_parses_plain_json(self):
        assert parse_json_reply('{"a": 1}') == {"a": 1}

    def test_parses_fenced_json_with_commentary(self):
        assert parse_json_reply('Here:\n```json\n{"a": 1}\n```') == {"a": 1}

    def test_rejects_a_json_array(self):
        with pytest.raises(ValueError):
            parse_json_reply('[1, 2, 3]')

    def test_raises_on_unparseable_text(self):
        with pytest.raises((ValueError, json.JSONDecodeError)):
            parse_json_reply("absolutely not json")


class ScriptedClient(OllamaClient):
    def __init__(self, replies):
        self.model = "test-model"
        self.host = ""
        self.timeout = 1
        self.temperature = 0.0
        self._client = None
        self._replies = list(replies)
        self.conversations = []

    def _chat(self, messages, json_mode=False):
        self.conversations.append([dict(m) for m in messages])
        return self._replies.pop(0)


class TestAskJsonRetries:
    def test_succeeds_first_time(self):
        client = ScriptedClient(['{"dashboard_title": "Sales"}'])
        assert client.ask_json("design it")["dashboard_title"] == "Sales"

    def test_retry_carries_the_original_request_and_the_bad_reply(self):
        client = ScriptedClient(["not json at all", '{"dashboard_title": "Sales"}'])
        result = client.ask_json("design it", system="be a BI analyst")

        assert result["dashboard_title"] == "Sales"

        retry = client.conversations[1]
        roles = [m["role"] for m in retry]
        assert roles == ["system", "user", "assistant", "user"]
        assert retry[1]["content"] == "design it"
        assert retry[2]["content"] == "not json at all"
        assert "could not be parsed" in retry[3]["content"]

    def test_gives_up_after_the_retry_budget(self):
        client = ScriptedClient(["bad", "still bad", "nope"])
        with pytest.raises(OllamaError) as excinfo:
            client.ask_json("design it", retries=2)
        assert "did not return valid JSON after 3 attempts" in str(excinfo.value)

    def test_recovers_from_a_fenced_reply_without_retrying(self):
        client = ScriptedClient(['```json\n{"dashboard_title": "Sales"}\n```'])
        assert client.ask_json("design it")["dashboard_title"] == "Sales"
        assert len(client.conversations) == 1
