"""What the MCP endpoint exposes, and what it deliberately does not.

The building tools were always few. The reading tools are the other half:
without them a client could create a sheet but never read a figure out of
one, so anything it said about the data was inferred from field names.

The line between them is the point of this file. Everything reachable over
MCP is read-only, because MCP cannot say who is calling - its handlers run
in tasks started from the server's lifespan, they share one Qlik session,
there is no confirmation step, and nothing they did would reach the audit
trail. A load-script rewrite that no record attributes to anybody is exactly
what this project must not have.
"""

import asyncio

import pytest

import mcp_server
from chat_tools import DESTRUCTIVE


def tool_names():
    return sorted(t.name for t in asyncio.run(mcp_server.mcp.list_tools()))


class TestWhatIsExposed:

    def test_the_building_tools_are_still_there(self):
        names = tool_names()
        for name in ("qlik_open", "qlik_data_sources", "qlik_build_sheet",
                     "qlik_save"):
            assert name in names

    def test_the_reading_tools_are_there_too(self):
        """An assistant that can build a chart but not read one is answering
        from field names."""
        names = tool_names()
        for name in ("qlik_query", "qlik_data_model", "qlik_script",
                     "qlik_list_charts", "qlik_check_expression",
                     "qlik_analyze_sheet"):
            assert name in names

    def test_every_tool_the_readme_promises_exists(self):
        """The README documented eleven tools that were never registered,
        which is why the endpoint read as broken rather than as small."""
        import re
        from pathlib import Path

        readme = Path(__file__).resolve().parent.parent / "README.md"
        promised = set(re.findall(r"qlik_[a-z_]+", readme.read_text(encoding="utf-8")))
        promised -= {"qlik_engine"}          # the module, not a tool
        missing = promised - set(tool_names())
        assert not missing, f"README promises tools that do not exist: {sorted(missing)}"


class TestNothingDestructiveIsReachable:

    def test_no_tool_can_change_the_app_s_data_or_script(self):
        """Reloading replaces every row; writing the script decides what
        every figure is made of. Neither may happen without a person behind
        it and a record of who."""
        assert mcp_server.READ_ONLY.isdisjoint(DESTRUCTIVE)

    def test_the_read_only_set_holds_no_writer(self):
        for name in ("reload_data", "write_script", "delete_sheet",
                     "create_chart", "edit_chart", "build_dashboard",
                     "add_data_source", "build_load_script"):
            assert name not in mcp_server.READ_ONLY

    def test_an_action_outside_the_set_is_refused_not_run(self, monkeypatch):
        """`allowed` is passed to execute() rather than assumed, so a typo
        here fails closed instead of quietly reaching a tool that writes."""
        class Engine:
            connected = True

            def reload_data(self):
                pytest.fail("a destructive action ran from the MCP endpoint")

        monkeypatch.setattr(mcp_server, "_require_engine", lambda: Engine())
        result = mcp_server._read("reload_data")
        assert "error" in result or result.get("refused") or result.get("not_allowed")


class TestReadingDelegatesToTheAssistantsTools:
    """One implementation, two front doors - rather than a second copy of
    `query` that drifts from the one the assistant uses."""

    @pytest.fixture
    def engine(self, monkeypatch):
        class Engine:
            connected = True
            app_name = "data"

            def get_script(self):
                return "///$tab Main\r\nTRACE hi;\r\n"

        engine = Engine()
        monkeypatch.setattr(mcp_server, "_require_engine", lambda: engine)
        return engine

    def test_it_calls_execute_with_the_action_and_arguments(self, engine,
                                                            monkeypatch):
        seen = {}

        def fake(engine_, name, arguments, allowed=None, confirm=None):
            seen.update(name=name, arguments=arguments, allowed=allowed)
            return {"rows": []}

        monkeypatch.setattr(mcp_server, "execute", fake)
        mcp_server.qlik_query(dimensions=["Region"],
                              measures=["Sum([Sales])"], limit=5)

        assert seen["name"] == "query"
        assert seen["arguments"] == {"dimensions": ["Region"],
                                     "measures": ["Sum([Sales])"], "limit": 5}
        assert seen["allowed"] == mcp_server.READ_ONLY

    def test_missing_lists_become_empty_rather_than_none(self, engine,
                                                         monkeypatch):
        """None reaches the engine as a null and fails somewhere less
        obvious than here."""
        seen = {}
        monkeypatch.setattr(
            mcp_server, "execute",
            lambda e, n, a, allowed=None, confirm=None: seen.update(a) or {})

        mcp_server.qlik_query()
        assert seen["dimensions"] == [] and seen["measures"] == []

    def test_an_engine_error_is_returned_rather_than_raised(self, engine,
                                                            monkeypatch):
        """A tool that raises takes the client's whole call down; one that
        answers with the reason can be acted on."""
        from qlik_engine import QlikEngineError

        def boom(*args, **kwargs):
            raise QlikEngineError("no app is open")

        monkeypatch.setattr(mcp_server, "execute", boom)
        assert "no app is open" in mcp_server.qlik_data_model()["error"]
