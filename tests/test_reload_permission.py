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
        assert "reload_data" in web_app.ASSISTANT_TOOLS


class TestRefusalIsHonest:
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
        assert DESTRUCTIVE == {"reload_data", "delete_sheet"}


class TestChatHonoursTheSetting:

    def test_permission_follows_the_session_setting(self, clean):
        session._state["engine"] = FakeEngine()
        session.set_allow_reload(False)

        confirm = lambda question: session.allow_reload()
        assert confirm("anything") is False

        session.set_allow_reload(True)
        assert confirm("anything") is True


class TestSystemPromptGuardsTheLoadScript:
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
    def test_the_switch_permits_a_reload(self, clean):
        session.set_allow_reload(True)
        may = web_app._may_run(session.system())
        assert may("reload_data replaces the data. Run it?", "reload_data") is True

    def test_the_switch_does_not_permit_a_script_overwrite(self, clean):
        session.set_allow_reload(True)
        may = web_app._may_run(session.system())
        assert may("write_script overwrites the whole script. Run it?",
                   "write_script") is False

    def test_switching_it_off_still_stops_a_reload(self, clean):
        session.set_allow_reload(False)
        may = web_app._may_run(session.system())
        assert may("anything", "reload_data") is False

    def test_the_switch_is_the_asking_user_s_own(self, clean):
        import users

        cautious = users.create("cautious", "test-password-1")
        theirs = session.for_user(cautious)
        theirs.set_allow_reload(False)
        session.set_allow_reload(True)

        assert web_app._may_run(session.system())("q", "reload_data") is True
        assert web_app._may_run(theirs)("q", "reload_data") is False

    def test_a_refused_overwrite_leaves_the_script_alone(self, clean):
        engine = FakeEngine()
        engine.set_script = lambda *a, **k: pytest.fail("the script was overwritten")

        result = execute(engine, "write_script",
                         {"content": "LOAD 1;", "mode": "replace_all"},
                         confirm=web_app._may_run(session.system()))

        assert result["cancelled"] is True
        assert "replace_tab" in result["note"]
