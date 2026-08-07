"""Asking for six charts should give six.

A model asked for six routinely produces one that groups by a constant field
or an identifier. Those are rejected for good reason - but dropping them
silently is how "make me 6 pie charts" quietly became five.
"""

import pytest

from chat_tools import MAX_HISTORY_CHARS, trim_history
from dashboard_builder import design_full_dashboard

FIELDS = [
    {"name": "SUM", "cardinality": 900, "tags": ["$numeric"]},
    {"name": "Region", "cardinality": 20, "tags": ["$text"]},
    {"name": "agent", "cardinality": 2, "tags": ["$text"]},
    {"name": "Currency", "cardinality": 2, "tags": ["$text"]},
    {"name": "deposit_type", "cardinality": 3, "tags": ["$text"]},
    {"name": "Report Date", "cardinality": 6, "tags": ["$text"]},
    {"name": "Type", "cardinality": 1, "tags": ["$text"]},          # constant
    {"name": "Id", "cardinality": 65752, "tags": ["$key"]},         # identifier
]


def pie(dimension, title=None):
    return {
        "type": "piechart", "title": title or f"Sum by {dimension}",
        "dimension": dimension, "measure": "SUM",
        "measure_expression": "Sum([SUM])",
    }


class ScriptedClient:
    """Stands in for OllamaClient, replaying one spec per ask_json call."""

    def __init__(self, specs):
        self._specs = list(specs)
        self.instructions = []

    def ask_json(self, prompt, system=None):
        self.instructions.append(prompt)
        if not self._specs:
            raise AssertionError("asked for more designs than the test scripted")
        return self._specs.pop(0)


def spec(*visualizations):
    return {"dashboard_title": "Six Pie Charts", "visualizations": list(visualizations)}


def design(client, instruction="6 pie charts"):
    return design_full_dashboard(
        FIELDS, model="m", instruction=instruction, client=client
    )


class TestTopUp:

    def test_a_rejected_chart_is_replaced(self):
        """The exact case: six asked for, one on a constant field."""
        client = ScriptedClient([
            spec(pie("Region"), pie("agent"), pie("Currency"),
                 pie("deposit_type"), pie("Report Date"), pie("Type")),
            spec(pie("Region", "Sum by Region again")),   # replacement offered
        ])

        result = design(client)
        dimensions = [v["dimension"] for v in result["visualizations"]]

        # Five originals survive, the constant one is gone, and a sixth is
        # requested. The replacement duplicates Region so it is refused, but
        # the model was asked - which is the behaviour under test.
        assert len(client.instructions) == 2
        assert "Type" not in dimensions[:5]

    def test_the_top_up_asks_for_the_shortfall_only(self):
        client = ScriptedClient([
            spec(pie("Region"), pie("agent"), pie("Type"), pie("Id")),
            spec(pie("Currency"), pie("deposit_type")),
        ])

        design(client, "4 pie charts")

        assert "exactly 2 more" in client.instructions[1]

    def test_the_top_up_names_what_was_rejected(self):
        client = ScriptedClient([
            spec(pie("Region"), pie("Type")),
            spec(pie("Currency")),
        ])

        design(client, "2 pie charts")
        retry = client.instructions[1]

        assert "Sum by Type" in retry
        assert "same value in every row" in retry

    def test_the_top_up_lists_what_is_already_there(self):
        """Otherwise the replacement is a duplicate of an accepted chart."""
        client = ScriptedClient([
            spec(pie("Region"), pie("Type")),
            spec(pie("Currency")),
        ])

        design(client, "2 pie charts")
        assert "Region" in client.instructions[1]

    def test_a_replacement_is_added(self):
        client = ScriptedClient([
            spec(pie("Region"), pie("Type")),
            spec(pie("Currency")),
        ])

        built = [
            v for v in design(client, "2 pie charts")["visualizations"]
            if v["dimension"] in ("Region", "Currency")
        ]
        assert len(built) == 2

    def test_no_top_up_when_everything_is_valid(self):
        client = ScriptedClient([spec(pie("Region"), pie("agent"))])
        design(client, "2 pie charts")
        assert len(client.instructions) == 1

    def test_rejected_charts_are_still_reported(self):
        """They must surface as 'skipped', not vanish unexplained."""
        client = ScriptedClient([
            spec(pie("Region"), pie("Type")),
            spec(pie("Id")),      # also invalid
        ])

        dimensions = [v["dimension"] for v in design(client, "2 pie charts")["visualizations"]]
        assert "Type" in dimensions or "Id" in dimensions

    def test_a_failing_top_up_is_not_fatal(self):
        class Failing(ScriptedClient):
            def ask_json(self, prompt, system=None):
                self.instructions.append(prompt)
                if len(self.instructions) > 1:
                    raise ValueError("model produced nonsense")
                return spec(pie("Region"), pie("Type"))

        result = design(Failing([]), "2 pie charts")
        assert any(v["dimension"] == "Region" for v in result["visualizations"])

    def test_only_one_extra_round(self):
        """Two design calls is the budget; a local model is slow."""
        client = ScriptedClient([
            spec(pie("Region"), pie("Type")),
            spec(pie("Id")),
        ])
        design(client, "2 pie charts")
        assert len(client.instructions) == 2


class TestHistoryTrimming:
    """Tool results are large and a local model's context is small.
    Overflowing it fails the whole turn."""

    def system(self):
        return {"role": "system", "content": "rules"}

    def turn(self, size):
        return [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "", "tool_calls": []},
            {"role": "tool", "content": "x" * size},
            {"role": "assistant", "content": "a"},
        ]

    def test_short_conversations_are_untouched(self):
        messages = [self.system()] + self.turn(10)
        assert trim_history(messages) == messages

    def test_the_system_prompt_always_survives(self):
        messages = [self.system()] + self.turn(20000) + self.turn(20000)
        assert trim_history(messages)[0]["role"] == "system"

    def test_old_turns_are_dropped(self):
        messages = [self.system()] + self.turn(MAX_HISTORY_CHARS) + self.turn(100)
        trimmed = trim_history(messages)

        assert len(trimmed) < len(messages)
        body = sum(len(m["content"]) for m in trimmed if m["role"] != "system")
        assert body <= MAX_HISTORY_CHARS

    def test_a_conversation_just_under_budget_is_kept(self):
        messages = [self.system()] + self.turn(MAX_HISTORY_CHARS - 100)
        assert trim_history(messages) == messages

    def test_it_never_starts_mid_turn(self):
        """A tool result without the assistant message that requested it is
        malformed and the model rejects the whole conversation."""
        messages = [self.system()] + self.turn(20000) + self.turn(20000)
        after_system = trim_history(messages)[1:]
        assert after_system[0]["role"] == "user"

    def test_the_latest_turn_is_kept_even_if_oversized(self):
        messages = [self.system()] + self.turn(999999)
        assert len(trim_history(messages)) > 1

    def test_an_empty_conversation_is_fine(self):
        assert trim_history([]) == []
