import pytest
from fastapi.testclient import TestClient

import glossary
import session
import web_app
from chat_tools import SYSTEM_PROMPT, system_prompt


@pytest.fixture(autouse=True)
def clean_session():
    session._state.update({"messages": [], "language": ""})
    yield
    session._state.update({"messages": [], "language": ""})


def test_no_language_leaves_the_assistant_following_the_question():
    assert system_prompt() == SYSTEM_PROMPT
    assert "ANSWER IN" not in system_prompt("")


@pytest.mark.parametrize("code,name", [("en", "ENGLISH"), ("ru", "RUSSIAN"), ("uz", "UZBEK")])
def test_each_offered_language_is_named_in_the_prompt(code, name):
    assert f"ANSWER IN {name}" in system_prompt(code)


def test_the_code_is_matched_however_it_is_written():
    assert "ANSWER IN UZBEK" in system_prompt(" UZ ")


def test_a_language_we_do_not_have_is_ignored_rather_than_pasted_in():
    assert "ANSWER IN" not in system_prompt("klingon")
    assert "ANSWER IN" not in system_prompt(None)


def test_pinning_a_language_does_not_licence_translating_the_data():
    prompt = system_prompt("uz")

    assert "never translated" in prompt
    assert "category labels" in prompt


def test_the_glossary_still_comes_first(monkeypatch, tmp_path):
    location = tmp_path / "glossary.md"
    location.write_text("FY starts in April.", encoding="utf-8")
    monkeypatch.setattr(glossary, "GLOSSARY_FILE", str(location))

    prompt = system_prompt("ru")
    assert prompt.index("FY starts in April.") < prompt.index("ANSWER IN RUSSIAN")
    assert SYSTEM_PROMPT in prompt


def test_setting_it_rewrites_the_conversation_already_open():
    session._state["messages"] = [
        {"role": "system", "content": system_prompt()},
        {"role": "user", "content": "hi"},
    ]
    session.set_language("uz")

    assert "ANSWER IN UZBEK" in session.messages()[0]["content"]
    assert session.messages()[1] == {"role": "user", "content": "hi"}


def test_clearing_it_puts_the_conversation_back():
    session._state["messages"] = [{"role": "system", "content": system_prompt("ru")}]
    session.set_language("")

    assert "ANSWER IN" not in session.messages()[0]["content"]
    assert session.language() == ""


def test_setting_it_before_a_conversation_exists_is_harmless():
    session.set_language("ru")
    assert session.language() == "ru"
    assert session.messages() == []


def test_a_new_chat_is_started_in_the_chosen_language():
    session.set_language("uz")
    session.reset_chat()

    assert "ANSWER IN UZBEK" in session.messages()[0]["content"]


@pytest.fixture
def client():
    session._state.update({
        "engine": None, "app_name": "data", "model": "test-model",
        "client": None, "messages": [], "language": "",
    })
    return TestClient(web_app.app)


def test_the_endpoint_sets_and_reports_it(client):
    assert client.post("/api/select", json={"language": "uz"}).json()["language"] == "uz"
    assert session.language() == "uz"


def test_switching_something_else_leaves_the_language_alone(client):
    session.set_language("ru")
    client.post("/api/select", json={"allow_reload": False})

    assert session.language() == "ru"


def test_an_empty_string_turns_it_off_rather_than_being_ignored(client):
    session.set_language("ru")
    client.post("/api/select", json={"language": ""})

    assert session.language() == ""
