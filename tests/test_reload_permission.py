"""Reloading: allowed by default, and refusable without lying about it.

Withholding reload outright produced a dead end - the model reported that
reloading was impossible and kept asking the user to do it by hand, four
times over, while the user kept saying "go ahead".
"""

import pytest

import session
import web_app
from chat_tools import DESTRUCTIVE, execute


class FakeEngine:
    connected = True
    mode = "desktop"

    def __init__(self):
        self.reloaded = False

    def reload_data(self):
        self.reloaded = True
        return {"success": True}

    def get_tables(self):
        return [{"name": "Orders", "rows": 100, "fields": []}]


@pytest.fixture
def clean():
    session._state.update({
        "engine": None, "app_name": None, "model": "test",
        "client": None, "messages": [], "allow_reload": True,
    })
    yield
    session._state.update({"engine": None, "app_name": None, "allow_reload": True})


class TestSetting:

    def test_reloading_is_allowed_by_default(self, clean):
        assert session.allow_reload() is True

    def test_can_be_switched_off(self, clean):
        assert session.set_allow_reload(False) is False
        assert session.allow_reload() is False

    def test_assistant_may_reload(self):
        """It is gated by a setting, not absent from the toolset."""
        assert "reload_data" in web_app.ASSISTANT_TOOLS


class TestRefusalIsHonest:
    """The failure that started this: refused, the model told the user
    reloading could not be done at all, instead of that it needed a click."""

    def refuse(self):
        return execute(FakeEngine(), "reload_data", {}, confirm=lambda q, action: False)

    def test_it_is_not_run(self):
        engine = FakeEngine()
        execute(engine, "reload_data", {}, confirm=lambda q, action: False)
        assert engine.reloaded is False

    def test_the_note_says_it_is_a_setting(self):
        note = self.refuse()["note"]
        assert "setting" in note
        assert "missing capability" in note

    def test_the_note_points_at_the_button(self):
        assert "Load data" in self.refuse()["note"]

    def test_the_note_offers_the_switch(self):
        assert "assistant may load data" in self.refuse()["note"]

    def test_it_runs_when_permitted(self):
        engine = FakeEngine()
        result = execute(engine, "reload_data", {}, confirm=lambda q, action: True)
        assert result["success"] is True
        assert engine.reloaded is True

    def test_only_the_two_unrecoverable_actions_are_gated(self):
        """Both throw away something that cannot be got back: a reload
        replaces every row, and deleting a sheet takes its charts with it.
        Everything else is either read-only or scoped to its own tab."""
        assert DESTRUCTIVE == {"reload_data", "delete_sheet"}


class TestChatHonoursTheSetting:

    def test_permission_follows_the_session_setting(self, clean):
        session._state["engine"] = FakeEngine()
        session.set_allow_reload(False)

        # This is the callback web_app hands to run_agent.
        confirm = lambda question: session.allow_reload()
        assert confirm("anything") is False

        session.set_allow_reload(True)
        assert confirm("anything") is True


class TestSystemPromptGuardsTheLoadScript:
    """The other half of that transcript: asked for four pie charts, it
    rewrote the load script and destroyed the data that was already there."""

    def test_prompt_tells_it_to_check_the_data_model_first(self):
        from chat_tools import SYSTEM_PROMPT
        assert "data_model" in SYSTEM_PROMPT

    def test_prompt_forbids_rewriting_the_script_for_a_chart_request(self):
        from chat_tools import SYSTEM_PROMPT
        lowered = SYSTEM_PROMPT.lower()
        assert "do not read or rewrite the load script when someone asks" in lowered

    def test_prompt_scopes_script_edits_to_data_requests(self):
        from chat_tools import SYSTEM_PROMPT
        assert "Only touch the load script when the user asks to load" in SYSTEM_PROMPT


class TestTheSwitchOnlyCoversLoading:
    """"Assistant may load data" is one permission, and the browser has no
    way to ask a second question mid-answer - so it was answering EVERY
    destructive confirmation, and a whole-script overwrite went through on
    the strength of a switch about loading data."""

    def test_the_switch_permits_a_reload(self, clean):
        session.set_allow_reload(True)
        assert web_app._may_run("reload_data replaces the data. Run it?",
                                "reload_data") is True

    def test_the_switch_does_not_permit_a_script_overwrite(self, clean):
        session.set_allow_reload(True)
        assert web_app._may_run("write_script overwrites the whole script. Run it?",
                                "write_script") is False

    def test_switching_it_off_still_stops_a_reload(self, clean):
        session.set_allow_reload(False)
        assert web_app._may_run("anything", "reload_data") is False

    def test_a_refused_overwrite_leaves_the_script_alone(self, clean):
        """Refused, not silently applied: the note steers the model to its
        own tab rather than telling the user it cannot write scripts."""
        engine = FakeEngine()
        engine.set_script = lambda *a, **k: pytest.fail("the script was overwritten")

        result = execute(engine, "write_script",
                         {"content": "LOAD 1;", "mode": "replace_all"},
                         confirm=web_app._may_run)

        assert result["cancelled"] is True
        assert "replace_tab" in result["note"]
