import json
from pathlib import Path

import httpx
import pytest

import llm
from llm import ModelError


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
        with pytest.raises(ModelError, match="rejected the API key"):
            client.chat(model="m", messages=[])

    def test_an_unknown_model_names_the_setting_to_fix(self, client):
        answer(client, {"error": {"message": "no such model"}}, status=404)
        with pytest.raises(ModelError, match="OPENAI_MODEL"):
            client.chat(model="m", messages=[])

    def test_rate_limiting_is_distinguishable(self, client):
        answer(client, {"error": {"message": "slow down"}}, status=429)
        with pytest.raises(ModelError, match="rate limiting"):
            client.chat(model="m", messages=[])

    def test_an_unreachable_endpoint_says_what_to_check(self, client):
        def handler(request):
            raise httpx.ConnectError("refused")

        client._http = fake_transport(handler)
        with pytest.raises(ModelError, match="OPENAI_BASE_URL"):
            client.chat(model="m", messages=[])

    def test_a_streaming_error_is_raised_not_swallowed(self, client):
        streamed(client, [], status=500)
        with pytest.raises(ModelError, match="returned 500"):
            list(client.chat(model="m", messages=[], stream=True))


class TestListingModels:

    def test_models_are_reported_in_the_shape_the_picker_reads(self, client):
        from llm import describe_models

        answer(client, {"data": [{"id": "gpt-4o"}, {"id": "gpt-4o-mini"}]})
        assert [m["name"] for m in describe_models(client)] == ["gpt-4o", "gpt-4o-mini"]

    def test_an_endpoint_without_a_models_route_still_offers_one(self, client):
        client.default_model = "local-model"
        answer(client, {"error": "not found"}, status=404)
        assert client.list() == {"models": [{"model": "local-model", "size": 0}]}

    def test_tool_support_is_assumed_when_the_endpoint_lists_nothing(self, client):
        """There is no OpenAI equivalent of Ollama's `show`, so with nothing to
        go on the answer has to be yes."""
        client._models = None
        assert "tools" in client.show("anything")["capabilities"]

    def test_a_model_the_endpoint_does_not_list_is_not_assumed(self, client):
        """This is what sent an Ollama model name to a Qwen gateway and got
        back `403 key not allowed to access model`. Claiming every name works
        means the wrong one is never caught here - it is caught by the
        endpoint, mid-question, in front of the user."""
        client._models = {"qwen-allowed"}
        assert client.show("phi4:14b")["capabilities"] == []

    def test_the_picker_accepts_a_hosted_model(self, client):
        from llm import pick_tool_model

        answer(client, {"data": [{"id": "gpt-4o"}]})
        chosen, _ = pick_tool_model(client, "gpt-4o")
        assert chosen == "gpt-4o"


class TestBuildingTheClient:

    def test_an_empty_base_url_is_refused_with_a_reason(self, monkeypatch):
        monkeypatch.setattr(llm, "_shared_client", None)
        monkeypatch.setattr(llm, "OPENAI_BASE_URL", "")
        with pytest.raises(ModelError, match="OPENAI_BASE_URL"):
            llm.build_client()

    def test_building_the_client(self, monkeypatch):
        monkeypatch.setattr(llm, "OPENAI_BASE_URL", "https://api.example.com/v1")
        assert isinstance(llm.build_client(), llm.OpenAICompatibleClient)

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

class TestTheConfiguredModelIsTheOneThatGetsSent:

    def chat_model_for(self, unused=None):
        """A clean subprocess, because config reads the project's own .env on
        import and this machine's happens to set CHAT_MODEL."""
        import os
        import subprocess
        import sys
        import tempfile

        root = str(Path(__file__).resolve().parent.parent)
        keep = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "COMSPEC", "PATHEXT")
        env = {k: v for k, v in os.environ.items() if k in keep}
        env.update({
            "OPENAI_MODEL": "qwen3.6-35b-a3b-fp8-dgx",
        })
        script = f"import sys; sys.path.insert(0, {root!r}); import config; print(config.CHAT_MODEL)"
        done = subprocess.run([sys.executable, "-c", script], env=env,
                              cwd=tempfile.gettempdir(), capture_output=True,
                              text=True, timeout=120)
        assert done.returncode == 0, done.stderr
        return done.stdout.strip()

    def test_chat_model_defaults_to_the_endpoint_s_model(self):
        assert self.chat_model_for() == "qwen3.6-35b-a3b-fp8-dgx", (
            "a model name other than OPENAI_MODEL reached the endpoint, which "
            "answers 403 when the key is scoped to one model")

    def test_a_model_the_endpoint_does_not_offer_is_not_vouched_for(self):
        client = llm.OpenAICompatibleClient(
            base_url="http://127.0.0.1:9/v1", default_model="qwen-allowed")
        client._models = {"qwen-allowed"}
        try:
            assert client.show("qwen-allowed")["capabilities"] != []
            assert client.show("phi4:14b")["capabilities"] == []
        finally:
            client.close()

    def test_the_configured_model_is_trusted_even_if_it_is_not_listed(self):
        """Some gateways serve a model without advertising it."""
        client = llm.OpenAICompatibleClient(
            base_url="http://127.0.0.1:9/v1", default_model="hidden-model")
        client._models = {"something-else"}
        try:
            assert client.show("hidden-model")["capabilities"] != []
        finally:
            client.close()

    def test_an_endpoint_that_lists_nothing_is_given_the_benefit_of_the_doubt(self):
        client = llm.OpenAICompatibleClient(
            base_url="http://127.0.0.1:9/v1", default_model="qwen-allowed")
        client._models = None
        try:
            assert client.show("anything-at-all")["capabilities"] != []
        finally:
            client.close()

    def test_the_endpoint_is_asked_for_its_models_only_once(self):
        client = llm.OpenAICompatibleClient(
            base_url="http://127.0.0.1:9/v1", default_model="qwen-allowed")
        calls = []

        def listing():
            calls.append(1)
            return {"models": [{"model": "qwen-allowed"}]}

        client.list = listing
        try:
            for _ in range(5):
                client.show("qwen-allowed")
            assert len(calls) == 1
        finally:
            client.close()

    def test_it_falls_through_to_a_model_the_endpoint_will_accept(self):
        from llm import pick_tool_model

        client = llm.OpenAICompatibleClient(
            base_url="http://127.0.0.1:9/v1", default_model="qwen-allowed")
        client.list = lambda: {"models": [{"model": "qwen-allowed"}]}
        try:
            chosen, note = pick_tool_model(client, "phi4:14b")
            assert chosen == "qwen-allowed"
            assert "phi4:14b" in note
        finally:
            client.close()
