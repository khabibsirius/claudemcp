import json

import httpx
import pytest

import llm
from ollama_client import OllamaError


def fake_transport(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(llm, "OPENAI_API_KEY", "test-key")
    return llm.OpenAICompatibleClient(base_url="https://api.example.com/v1",
                                      api_key="test-key")


def answer(client, body, status=200):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content) if request.content else None
        return httpx.Response(status, json=body)

    client._http = fake_transport(handler)
    return seen


def streamed(client, chunks, status=200):
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        text = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
        return httpx.Response(status, text=text,
                              headers={"content-type": "text/event-stream"})

    client._http = fake_transport(handler)
    return seen


class TestTranslatingMessagesOut:

    def test_plain_messages_pass_through(self, client):
        seen = answer(client, {"choices": [{"message": {"content": "hi"}}]})
        client.chat(model="m", messages=[
            {"role": "system", "content": "rules"},
            {"role": "user", "content": "hello"},
        ])
        assert seen["body"]["messages"] == [
            {"role": "system", "content": "rules"},
            {"role": "user", "content": "hello"},
        ]

    def test_tool_arguments_become_a_json_string(self, client):
        seen = answer(client, {"choices": [{"message": {"content": ""}}]})
        client.chat(model="m", messages=[
            {"role": "assistant", "content": "",
             "tool_calls": [{"function": {"name": "query",
                                          "arguments": {"limit": 5}}}]},
            {"role": "tool", "tool_name": "query", "content": "{}"},
        ])
        call = seen["body"]["messages"][0]["tool_calls"][0]
        assert call["type"] == "function"
        assert call["function"]["arguments"] == '{"limit": 5}'

    def test_a_tool_result_quotes_the_call_it_answers(self, client):
        seen = answer(client, {"choices": [{"message": {"content": ""}}]})
        client.chat(model="m", messages=[
            {"role": "assistant", "content": "",
             "tool_calls": [{"function": {"name": "query", "arguments": {}}}]},
            {"role": "tool", "tool_name": "query", "content": "rows"},
        ])
        assistant, result = seen["body"]["messages"]
        assert result["tool_call_id"] == assistant["tool_calls"][0]["id"]

    def test_two_calls_in_one_turn_are_matched_by_name(self, client):
        seen = answer(client, {"choices": [{"message": {"content": ""}}]})
        client.chat(model="m", messages=[
            {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "query", "arguments": {}}},
                {"function": {"name": "data_model", "arguments": {}}},
            ]},
            {"role": "tool", "tool_name": "data_model", "content": "b"},
            {"role": "tool", "tool_name": "query", "content": "a"},
        ])
        assistant, first, second = seen["body"]["messages"]
        ids = {c["function"]["name"]: c["id"] for c in assistant["tool_calls"]}
        assert first["tool_call_id"] == ids["data_model"]
        assert second["tool_call_id"] == ids["query"]

    def test_an_unmatched_result_still_answers_something(self, client):
        seen = answer(client, {"choices": [{"message": {"content": ""}}]})
        client.chat(model="m", messages=[
            {"role": "assistant", "content": "",
             "tool_calls": [{"function": {"name": "query", "arguments": {}}}]},
            {"role": "tool", "tool_name": "something_else", "content": "x"},
        ])
        assistant, result = seen["body"]["messages"]
        assert result["tool_call_id"] == assistant["tool_calls"][0]["id"]

    def test_an_assistant_turn_with_calls_sends_null_not_empty_content(self, client):
        seen = answer(client, {"choices": [{"message": {"content": ""}}]})
        client.chat(model="m", messages=[
            {"role": "assistant", "content": "",
             "tool_calls": [{"function": {"name": "query", "arguments": {}}}]},
        ])
        assert seen["body"]["messages"][0]["content"] is None

    def test_tools_are_forwarded_only_when_there_are_some(self, client):
        seen = answer(client, {"choices": [{"message": {"content": ""}}]})
        client.chat(model="m", messages=[{"role": "user", "content": "hi"}])
        assert "tools" not in seen["body"]

        seen = answer(client, {"choices": [{"message": {"content": ""}}]})
        client.chat(model="m", messages=[{"role": "user", "content": "hi"}],
                    tools=[{"type": "function", "function": {"name": "query"}}])
        assert seen["body"]["tools"][0]["function"]["name"] == "query"


class TestTranslatingOptions:

    def test_temperature_carries_over(self, client):
        seen = answer(client, {"choices": [{"message": {"content": ""}}]})
        client.chat(model="m", messages=[], options={"temperature": 0.2})
        assert seen["body"]["temperature"] == 0.2

    def test_num_ctx_is_dropped(self, client):
        seen = answer(client, {"choices": [{"message": {"content": ""}}]})
        client.chat(model="m", messages=[],
                    options={"temperature": 0.2, "num_ctx": 32768})
        assert "num_ctx" not in seen["body"]

    def test_num_predict_becomes_max_tokens(self, client):
        seen = answer(client, {"choices": [{"message": {"content": ""}}]})
        client.chat(model="m", messages=[], options={"num_predict": 900})
        assert seen["body"]["max_tokens"] == 900


class TestAuthenticationHeaders:

    def test_the_key_is_sent_as_a_bearer_token(self):
        c = llm.OpenAICompatibleClient(base_url="https://x/v1", api_key="sk-test")
        assert c._http.headers["authorization"] == "Bearer sk-test"

    def test_extra_headers_are_added(self):
        c = llm.OpenAICompatibleClient(
            base_url="https://x/v1", api_key="k",
            extra_headers={"HTTP-Referer": "https://bank.example"})
        assert c._http.headers["http-referer"] == "https://bank.example"

    def test_no_key_means_no_authorization_header(self):
        c = llm.OpenAICompatibleClient(base_url="http://vllm:8000/v1", api_key="")
        assert "authorization" not in c._http.headers


class TestTranslatingRepliesBack:

    def test_a_plain_reply_is_ollama_shaped(self, client):
        answer(client, {"choices": [{"message": {"content": "About 4.2bn."},
                                     "finish_reason": "stop"}]})
        response = client.chat(model="m", messages=[])
        assert response["message"]["content"] == "About 4.2bn."
        assert response["message"]["tool_calls"] == []

    def test_tool_calls_come_back_in_the_shape_the_agent_loop_reads(self, client):
        answer(client, {"choices": [{"message": {"content": None, "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "query", "arguments": '{"limit": 5}'}},
        ]}}]})
        response = client.chat(model="m", messages=[])
        call = response["message"]["tool_calls"][0]
        assert call["function"]["name"] == "query"
        assert call["function"]["arguments"] == '{"limit": 5}'

    def test_the_agent_loop_can_actually_read_it(self, client):
        from chat_tools import _parse_arguments, _plain_calls

        answer(client, {"choices": [{"message": {"content": None, "tool_calls": [
            {"id": "c1", "function": {"name": "query",
                                      "arguments": '{"limit": 5}'}}]}}]})
        response = client.chat(model="m", messages=[])

        plain = _plain_calls(response["message"]["tool_calls"])
        arguments, error = _parse_arguments("query", plain[0]["function"]["arguments"])
        assert error is None
        assert arguments == {"limit": 5}

    def test_a_missing_content_becomes_an_empty_string(self, client):
        answer(client, {"choices": [{"message": {"content": None}}]})
        assert client.chat(model="m", messages=[])["message"]["content"] == ""


class TestStreaming:

    def test_text_arrives_as_chunks(self, client):
        streamed(client, [
            {"choices": [{"delta": {"content": "About "}}]},
            {"choices": [{"delta": {"content": "4.2bn."}}]},
        ])
        chunks = list(client.chat(model="m", messages=[], stream=True))
        text = "".join(c["message"]["content"] for c in chunks)
        assert text == "About 4.2bn."

    def test_fragmented_tool_calls_are_assembled_into_one(self, client):
        streamed(client, [
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "c1", "function": {"name": "qu", "arguments": ""}}]}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"name": "ery", "arguments": '{"li'}}]}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": 'mit": 5}'}}]}}]},
        ])
        chunks = list(client.chat(model="m", messages=[], stream=True))

        calls = [c for chunk in chunks for c in chunk["message"]["tool_calls"]]
        assert len(calls) == 1
        assert calls[0]["function"]["name"] == "query"
        assert json.loads(calls[0]["function"]["arguments"]) == {"limit": 5}

    def test_two_parallel_calls_stay_separate(self, client):
        streamed(client, [
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "a", "function": {"name": "query", "arguments": "{}"}},
                {"index": 1, "id": "b", "function": {"name": "data_model", "arguments": "{}"}},
            ]}}]},
        ])
        chunks = list(client.chat(model="m", messages=[], stream=True))
        names = [c["function"]["name"]
                 for chunk in chunks for c in chunk["message"]["tool_calls"]]
        assert names == ["query", "data_model"]

    def test_the_done_marker_ends_the_stream(self, client):
        streamed(client, [{"choices": [{"delta": {"content": "x"}}]}])
        chunks = list(client.chat(model="m", messages=[], stream=True))
        assert chunks[-1]["done"] is True

    def test_an_unparseable_chunk_is_skipped_not_fatal(self, client):
        def handler(request):
            text = ('data: {"choices":[{"delta":{"content":"a"}}]}\n\n'
                    'data: {not json\n\n'
                    'data: {"choices":[{"delta":{"content":"b"}}]}\n\n'
                    'data: [DONE]\n\n')
            return httpx.Response(200, text=text)

        client._http = fake_transport(handler)
        chunks = list(client.chat(model="m", messages=[], stream=True))
        assert "".join(c["message"]["content"] for c in chunks) == "ab"


class TestErrors:

    def test_a_bad_key_says_so(self, client):
        answer(client, {"error": {"message": "Incorrect API key"}}, status=401)
        with pytest.raises(OllamaError, match="rejected the API key"):
            client.chat(model="m", messages=[])

    def test_an_unknown_model_names_the_setting_to_fix(self, client):
        answer(client, {"error": {"message": "no such model"}}, status=404)
        with pytest.raises(OllamaError, match="OPENAI_MODEL"):
            client.chat(model="m", messages=[])

    def test_rate_limiting_is_distinguishable(self, client):
        answer(client, {"error": {"message": "slow down"}}, status=429)
        with pytest.raises(OllamaError, match="rate limiting"):
            client.chat(model="m", messages=[])

    def test_an_unreachable_endpoint_says_what_to_check(self, client):
        def handler(request):
            raise httpx.ConnectError("refused")

        client._http = fake_transport(handler)
        with pytest.raises(OllamaError, match="OPENAI_BASE_URL"):
            client.chat(model="m", messages=[])

    def test_a_streaming_error_is_raised_not_swallowed(self, client):
        streamed(client, [], status=500)
        with pytest.raises(OllamaError, match="returned 500"):
            list(client.chat(model="m", messages=[], stream=True))


class TestListingModels:

    def test_models_are_reported_in_the_shape_the_picker_reads(self, client):
        from ollama_client import describe_models

        answer(client, {"data": [{"id": "gpt-4o"}, {"id": "gpt-4o-mini"}]})
        assert [m["name"] for m in describe_models(client)] == ["gpt-4o", "gpt-4o-mini"]

    def test_an_endpoint_without_a_models_route_still_offers_one(self, client):
        client.default_model = "local-model"
        answer(client, {"error": "not found"}, status=404)
        assert client.list() == {"models": [{"model": "local-model", "size": 0}]}

    def test_tool_support_is_assumed(self, client):
        assert "tools" in client.show("anything")["capabilities"]

    def test_the_picker_accepts_a_hosted_model(self, client):
        from ollama_client import pick_tool_model

        answer(client, {"data": [{"id": "gpt-4o"}]})
        chosen, _ = pick_tool_model(client, "gpt-4o")
        assert chosen == "gpt-4o"


class TestProviderSelection:

    @pytest.mark.parametrize("configured,expected", [
        ("ollama", llm.OLLAMA), ("", llm.OLLAMA), ("OLLAMA", llm.OLLAMA),
        ("openai", llm.OPENAI), ("openrouter", llm.OPENAI),
        ("azure", llm.OPENAI), ("vllm", llm.OPENAI),
        ("openai-compatible", llm.OPENAI),
    ])
    def test_the_setting_picks_the_client(self, monkeypatch, configured, expected):
        monkeypatch.setattr(llm, "LLM_PROVIDER", configured)
        assert llm.provider() == expected

    def test_an_unknown_provider_falls_back_to_local(self, monkeypatch):
        monkeypatch.setattr(llm, "LLM_PROVIDER", "something-else")
        assert llm.provider() == llm.OLLAMA

    def test_openai_without_a_base_url_is_refused_with_a_reason(self, monkeypatch):
        monkeypatch.setattr(llm, "LLM_PROVIDER", "openai")
        monkeypatch.setattr(llm, "OPENAI_BASE_URL", "")
        with pytest.raises(OllamaError, match="OPENAI_BASE_URL"):
            llm.build_client()

    def test_building_an_openai_client(self, monkeypatch):
        monkeypatch.setattr(llm, "LLM_PROVIDER", "openai")
        monkeypatch.setattr(llm, "OPENAI_BASE_URL", "https://api.example.com/v1")
        assert isinstance(llm.build_client(), llm.OpenAICompatibleClient)

    def test_the_default_model_follows_the_provider(self, monkeypatch):
        monkeypatch.setattr(llm, "LLM_PROVIDER", "openai")
        monkeypatch.setattr(llm, "OPENAI_MODEL", "gpt-4o")
        assert llm.default_model() == "gpt-4o"

        monkeypatch.setattr(llm, "LLM_PROVIDER", "ollama")
        from config import CHAT_MODEL
        assert llm.default_model() == CHAT_MODEL


class TestTheSessionUsesIt:

    def test_a_session_builds_whatever_the_setting_says(self, monkeypatch):
        import session

        built = []
        monkeypatch.setattr(session.llm, "build_client",
                            lambda: built.append(1) or "a-client")
        sess = session.Session("test")
        assert sess.client() == "a-client"
        assert sess.client() == "a-client"
        assert len(built) == 1


class TestTheMonologueNeverReachesTheUser:
    def test_a_complete_block_is_removed(self):
        assert llm.strip_thinking(
            "<think>The user wants deposits. Let me consider.</think>"
            "Deposits total 4.2bn."
        ) == "Deposits total 4.2bn."

    def test_several_blocks_are_removed(self):
        assert llm.strip_thinking(
            "<think>one</think>A<think>two</think>B") == "AB"

    def test_text_without_any_is_untouched(self):
        assert llm.strip_thinking("Deposits total 4.2bn.") == "Deposits total 4.2bn."

    def test_an_unterminated_block_takes_the_rest_with_it(self):
        assert llm.strip_thinking(
            "Here: <think>still thinking and then cut") == "Here: "

    def test_empty_and_none_survive(self):
        assert llm.strip_thinking("") == ""
        assert llm.strip_thinking(None) is None

    def test_a_buffered_reply_is_cleaned(self, client):
        answer(client, {"choices": [{"message": {
            "content": "<think>hmm</think>Deposits total 4.2bn."}}]})
        assert client.chat(model="m", messages=[])["message"]["content"] == (
            "Deposits total 4.2bn.")

    def test_a_reasoning_field_is_ignored_entirely(self, client):
        answer(client, {"choices": [{"message": {
            "content": "Deposits total 4.2bn.",
            "reasoning_content": "The user is asking about deposits...",
        }}]})
        response = client.chat(model="m", messages=[])
        assert response["message"]["content"] == "Deposits total 4.2bn."
        assert "asking about deposits" not in str(response)


class TestTheMonologueIsFilteredOutOfAStream:
    def emitted(self, client, pieces):
        streamed(client, [{"choices": [{"delta": {"content": p}}]} for p in pieces])
        return "".join(c["message"]["content"]
                       for c in client.chat(model="m", messages=[], stream=True))

    def test_a_block_arriving_whole_is_dropped(self, client):
        assert self.emitted(client, ["<think>hmm</think>", "Deposits ", "4.2bn."]) == (
            "Deposits 4.2bn.")

    def test_a_tag_split_across_chunks_is_still_recognised(self, client):
        assert self.emitted(
            client, ["<th", "ink>", "hmm", "</thi", "nk>", "Deposits 4.2bn."]
        ) == "Deposits 4.2bn."

    def test_one_character_at_a_time(self, client):
        text = "<think>reasoning here</think>The answer."
        assert self.emitted(client, list(text)) == "The answer."

    def test_text_before_and_after_survives(self, client):
        assert self.emitted(
            client, ["Before ", "<think>x</think>", "after."]) == "Before after."

    def test_ordinary_angle_brackets_are_not_eaten(self, client):
        assert self.emitted(client, ["Sales < 5 and ", "revenue > 10"]) == (
            "Sales < 5 and revenue > 10")

    def test_nothing_is_held_back_at_the_end(self, client):
        assert self.emitted(client, ["Deposits 4.2bn."]) == "Deposits 4.2bn."

    def test_a_stream_that_stops_mid_thought_emits_nothing(self, client):
        assert self.emitted(client, ["<think>still thinking"]) == ""

    def test_the_filter_is_per_stream_not_shared(self, client):
        self.emitted(client, ["<think>unfinished"])
        assert self.emitted(client, ["A clean answer."]) == "A clean answer."


class TestEndpointSpecificBodyParameters:

    def test_extra_body_is_merged_into_the_request(self, client, monkeypatch):
        monkeypatch.setattr(
            llm, "OPENAI_EXTRA_BODY",
            {"chat_template_kwargs": {"enable_thinking": False}})
        seen = answer(client, {"choices": [{"message": {"content": ""}}]})
        client.chat(model="m", messages=[])
        assert seen["body"]["chat_template_kwargs"] == {"enable_thinking": False}

    def test_nothing_is_added_when_it_is_empty(self, client, monkeypatch):
        monkeypatch.setattr(llm, "OPENAI_EXTRA_BODY", {})
        seen = answer(client, {"choices": [{"message": {"content": ""}}]})
        client.chat(model="m", messages=[])
        assert set(seen["body"]) == {"model", "messages", "stream"}

    @pytest.mark.parametrize("raw", ["not json", "[1, 2]", '"a string"', "7"])
    def test_a_malformed_setting_is_refused_with_a_reason(self, monkeypatch, raw):
        import config

        monkeypatch.setenv("_TEST_BODY", raw)
        with pytest.raises(config.ConfigError, match="JSON object"):
            config._json_env("_TEST_BODY")

    def test_an_empty_setting_is_simply_empty(self, monkeypatch):
        import config

        monkeypatch.delenv("_TEST_BODY", raising=False)
        assert config._json_env("_TEST_BODY") == {}
