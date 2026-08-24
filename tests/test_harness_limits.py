"""The harness must not be the reason the model looks stupid.

Three quiet failure modes, all invisible to the person watching:

  * No num_ctx on a call -> Ollama uses its own small default and silently
    truncates the OLDEST part of an oversized prompt, which is the system
    prompt carrying every rule.
  * No temperature on a tool-calling turn -> Ollama's default (~0.8) is
    where invented tool arguments come from.
  * A history budget sized for a 7B model -> a large-context model forgets
    the user's request two tool calls in.
"""

import config
from chat_tools import (
    AGENT_OPTIONS,
    MAX_HISTORY_CHARS,
    SYSTEM_PROMPT,
    run_agent,
    stream_agent,
)


class FakeEngine:
    connected = True


class RecordingClient:
    def __init__(self):
        self.options = []

    def chat(self, model, messages, tools=None, stream=False, options=None):
        self.options.append(options)
        reply = {"message": {"content": "done", "tool_calls": []}}
        return iter([reply]) if stream else reply


class TestEveryCallCarriesOptions:

    def test_run_agent_sends_num_ctx_and_a_cool_temperature(self):
        client = RecordingClient()
        run_agent(client, "m", FakeEngine(), [])

        options = client.options[0]
        assert options["num_ctx"] == config.OLLAMA_NUM_CTX
        assert options["temperature"] <= 0.3

    def test_stream_agent_sends_the_same(self):
        client = RecordingClient()
        list(stream_agent(client, "m", FakeEngine(), []))

        assert client.options[0] == AGENT_OPTIONS

    def test_the_design_client_sends_num_ctx(self, monkeypatch):
        import ollama_client

        seen = {}

        class FakeInner:
            def chat(self, model, messages, format=None, options=None):
                seen["options"] = options
                return {"message": {"content": "{}"}}

        client = ollama_client.OllamaClient(model="m")
        client._client = FakeInner()
        client.ask_json("design")

        assert seen["options"]["num_ctx"] == config.OLLAMA_NUM_CTX


class TestBudgetsScaleWithTheContextWindow:

    def test_history_budget_is_at_least_the_old_floor(self):
        assert MAX_HISTORY_CHARS >= 24_000

    def test_history_budget_tracks_num_ctx(self):
        """With a real context window configured, the history budget must
        grow with it rather than staying at the 7B-era constant.

        The reserve subtracted here is what the system prompt, the tool
        schemas and the reply need. It was 6,000 while the prompt was ~1,900
        tokens; the prompt is now ~5,100 and the schemas ~2,100, so 6,000 no
        longer covers the fixed overhead and the window overflows before the
        conversation starts. test_the_reserve_covers_the_real_overhead below
        is what keeps this number honest as the prompt changes.
        """
        reserve = config.CHAT_HISTORY_RESERVE
        assert config.CHAT_HISTORY_CHARS >= (config.OLLAMA_NUM_CTX - reserve) * 3

    def test_the_reserve_covers_the_real_overhead(self):
        """The reserve is measured against the actual prompt, not guessed.

        Grow SYSTEM_PROMPT past what CHAT_HISTORY_RESERVE allows for and this
        fails here, rather than as Ollama silently truncating the front of
        the prompt in production and deleting every rule the model has.
        """
        import json

        from chat_tools import SYSTEM_PROMPT, TOOLS

        # ~4 characters per token for English prose and JSON schemas.
        fixed = (len(SYSTEM_PROMPT) + len(json.dumps(TOOLS))) // 4

        # The reply needs room too. An inventory briefing puts EVERY chart
        # on its own numbered row with its own figures, so a 34-chart app is
        # 34 rows plus an overall picture. A table cut off at row 19 reads as
        # "the app has 19 charts", which is worse than no answer at all.
        REPLY_ALLOWANCE = 7_000

        assert config.CHAT_HISTORY_RESERVE >= fixed + REPLY_ALLOWANCE, (
            f"fixed overhead is ~{fixed:,} tokens and a long reply needs "
            f"~{REPLY_ALLOWANCE:,} more, but only "
            f"{config.CHAT_HISTORY_RESERVE:,} is reserved - either trim "
            f"SYSTEM_PROMPT or raise CHAT_HISTORY_RESERVE"
        )

    def test_step_cap_allows_a_real_load_and_chart_flow(self):
        assert config.CHAT_MAX_STEPS >= 25


class TestPromptScaffoldsReasoning:
    """The workflow the prompt teaches, not the model, is what turns "the
    data is not enough" into a proposal instead of a refusal."""

    def test_it_tells_the_model_to_plan(self):
        assert "PLAN multi-step work" in SYSTEM_PROMPT

    def test_it_handles_insufficient_data_by_proposing_a_fix(self):
        assert "WHEN THE DATA IS NOT ENOUGH" in SYSTEM_PROMPT
        assert "Say exactly what is missing" in SYSTEM_PROMPT
        assert "Ask the user first" in SYSTEM_PROMPT

    def test_it_still_guards_the_load_script_for_plain_chart_requests(self):
        lowered = SYSTEM_PROMPT.lower()
        assert "do not read or rewrite the load script when someone asks" in lowered

    def test_complex_script_work_is_allowed_with_the_guardrails(self):
        assert "you MAY write the Qlik script yourself" in SYSTEM_PROMPT
        assert "Never rewrite or delete a tab you did not create" in SYSTEM_PROMPT

    def test_richer_measures_are_offered_for_create_chart(self):
        assert "set analysis" in SYSTEM_PROMPT
        assert "check_expression" in SYSTEM_PROMPT
