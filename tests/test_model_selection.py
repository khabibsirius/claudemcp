import pytest

from llm import (
    ModelError,
    model_capabilities,
    pick_tool_model,
    supports_tools,
    tool_capable_models,
)

CAPABILITIES = {
    "phi4:14b": ["completion"],
    "deepseek-coder-v2:16b": ["completion", "insert"],
    "gemma4:26b": ["completion", "vision", "tools", "thinking"],
    "qwen2.5-coder:7b": ["completion", "tools", "insert"],
    "kimi-k2.5:cloud": ["completion", "tools"],
    "gpt-oss:120b-cloud": ["completion", "tools"],
    "tiny/nexus:1b": ["completion", "tools"],
}


class FakeClient:
    def __init__(self, capabilities=None, listing=None, show_fails=()):
        self.capabilities = CAPABILITIES if capabilities is None else capabilities
        self._listing = listing if listing is not None else list(self.capabilities)
        self.show_fails = set(show_fails)
        self.loaded = []

    def list(self):
        return {"models": [{"model": name} for name in self._listing]}

    def show(self, model):
        self.loaded.append(model)
        if model in self.show_fails:
            raise RuntimeError("no such model")
        return {"capabilities": self.capabilities.get(model, [])}


class TestCapabilityDetection:

    def test_reads_declared_capabilities(self):
        assert "tools" in model_capabilities(FakeClient(), "gemma4:26b")

    def test_unknown_model_has_no_capabilities(self):
        assert model_capabilities(FakeClient(show_fails=["ghost"]), "ghost") == []

    @pytest.mark.parametrize("model,expected", [
        ("gemma4:26b", True),
        ("qwen2.5-coder:7b", True),
        ("phi4:14b", False),
        ("deepseek-coder-v2:16b", False),
    ])
    def test_supports_tools(self, model, expected):
        assert supports_tools(FakeClient(), model) is expected

    def test_detection_does_not_load_the_model(self):
        client = FakeClient()
        supports_tools(client, "gemma4:26b")
        assert client.loaded == ["gemma4:26b"]

    def test_client_has_no_chat_method_in_this_test(self):
        assert not hasattr(FakeClient(), "chat")


class TestCandidateRanking:

    def test_lists_only_tool_capable_models(self):
        candidates = tool_capable_models(FakeClient())
        assert "phi4:14b" not in candidates
        assert "gemma4:26b" in candidates

    def test_cloud_models_rank_last(self):
        candidates = tool_capable_models(FakeClient())
        assert candidates.index("kimi-k2.5:cloud") > candidates.index("gemma4:26b")

    def test_hyphenated_cloud_names_also_rank_last(self):
        candidates = tool_capable_models(FakeClient())
        assert candidates.index("gpt-oss:120b-cloud") > candidates.index("gemma4:26b")

    def test_a_hosted_model_is_never_auto_selected(self):
        assert pick_tool_model(FakeClient(), "phi4:14b")[0] == "gemma4:26b"

    def test_tiny_models_rank_below_real_ones(self):
        candidates = tool_capable_models(FakeClient())
        assert candidates.index("tiny/nexus:1b") > candidates.index("gemma4:26b")


class TestPickToolModel:

    def test_keeps_a_capable_preference(self):
        model, note = pick_tool_model(FakeClient(), "gemma4:26b")
        assert model == "gemma4:26b"
        assert note == ""

    def test_falls_back_when_the_preference_cannot_call_tools(self):
        model, note = pick_tool_model(FakeClient(), "phi4:14b")

        assert model == "gemma4:26b"
        assert "phi4:14b" in note and "gemma4:26b" in note

    def test_the_note_says_how_to_choose(self):
        _, note = pick_tool_model(FakeClient(), "phi4:14b")
        assert "OPENAI_MODEL" in note

    def test_raises_when_nothing_installed_can_call_tools(self):
        client = FakeClient(capabilities={"phi4:14b": ["completion"]})
        with pytest.raises(ModelError, match="OPENAI_MODEL"):
            pick_tool_model(client, "phi4:14b")

    def test_handles_an_empty_preference(self):
        assert pick_tool_model(FakeClient(), "")[0] == "gemma4:26b"

    def test_an_unreachable_model_host_is_reported_without_naming_ollama(self):
        class Dead:
            def list(self):
                raise ConnectionError("connection refused")

            def show(self, model):
                raise ConnectionError("connection refused")

        with pytest.raises(ModelError) as raised:
            pick_tool_model(Dead(), "phi4:14b")

        message = str(raised.value)
        assert "connection refused" in message
        assert "ollama" not in message.lower(), (
            "this path serves both providers, so telling somebody running "
            "Qwen to start Ollama sends them after the wrong thing")
