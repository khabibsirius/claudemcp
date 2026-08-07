"""Tab-level editing of the load script.

The Data load editor is organised into tabs. Editing one tab rather than the
whole script is what makes AI editing safe to repeat: the model rewrites its
own section without ever touching what someone wrote by hand.
"""

import pytest

from data_prep import (
    GENERATED_TAB,
    append_to_tab,
    delete_tab,
    describe_data_model,
    get_tab,
    set_tab,
    split_tabs,
    tab_names,
)

SCRIPT = (
    "///$tab Main\r\nLOAD * FROM [lib://dataset/*.csv];\r\n"
    "///$tab other\r\nLIB CONNECT TO 'rest';\r\n"
)


class TestReadingTabs:

    def test_lists_tab_names(self):
        assert tab_names(SCRIPT) == ["Main", "other"]

    def test_reads_one_tab(self):
        assert "LIB CONNECT TO 'rest';" in get_tab(SCRIPT, "other")

    def test_unknown_tab_is_none(self):
        assert get_tab(SCRIPT, "nope") is None

    def test_tab_lookup_is_case_insensitive(self):
        assert get_tab(SCRIPT, "OTHER") is not None


class TestSetTab:

    def test_creates_a_new_tab(self):
        result = set_tab(SCRIPT, "LOAD 1 AS x AUTOGENERATE 1;")
        assert tab_names(result) == ["Main", "other", GENERATED_TAB]

    def test_leaves_hand_written_tabs_alone(self):
        result = set_tab(SCRIPT, "LOAD 1 AS x AUTOGENERATE 1;")
        assert "LIB CONNECT TO 'rest';" in result
        assert "LOAD * FROM [lib://dataset/*.csv];" in result

    def test_overwrites_an_existing_tab(self):
        result = set_tab(SCRIPT, "LOAD 2 AS y AUTOGENERATE 1;", tab_name="Main")
        assert "AS y" in result
        assert "lib://dataset" not in result
        assert tab_names(result) == ["Main", "other"]

    def test_repeated_edits_do_not_stack_up(self):
        once = set_tab(SCRIPT, "LOAD 1 AS a AUTOGENERATE 1;")
        twice = set_tab(once, "LOAD 2 AS b AUTOGENERATE 1;")
        assert tab_names(twice).count(GENERATED_TAB) == 1
        assert "AS b" in twice and "AS a" not in twice


class TestAppendToTab:

    def test_keeps_the_existing_body(self):
        result = append_to_tab(SCRIPT, "TRACE done;", tab_name="Main")
        body = get_tab(result, "Main")
        assert "LOAD * FROM" in body and "TRACE done;" in body

    def test_creates_the_tab_when_absent(self):
        result = append_to_tab(SCRIPT, "TRACE hello;", tab_name="New")
        assert "TRACE hello;" in get_tab(result, "New")


class TestDeleteTab:

    def test_removes_the_tab(self):
        result = delete_tab(SCRIPT, "other")
        assert tab_names(result) == ["Main"]
        assert "LIB CONNECT" not in result

    def test_unknown_tab_is_a_no_op(self):
        assert split_tabs(delete_tab(SCRIPT, "nope")) == split_tabs(SCRIPT)

    def test_deleting_the_only_tab_gives_an_empty_script(self):
        assert delete_tab("///$tab Main\r\nLOAD 1;\r\n", "Main") == ""


class FakeModelEngine:
    def __init__(self, tables, fields=None, fields_error=None):
        self._tables = tables
        self._fields = fields or []
        self._fields_error = fields_error

    def get_tables(self):
        return self._tables

    def get_fields(self):
        if self._fields_error:
            raise self._fields_error
        return self._fields


class TestDescribeDataModel:
    """One call has to answer both 'how is the data shaped' and 'can I chart
    this field', which the engine reports separately."""

    TABLES = [{
        "name": "Orders", "rows": 100,
        "fields": [
            {"name": "Sales", "distinct": None, "density": 1.0, "null_count": 0},
            {"name": "Region", "distinct": 5, "density": 1.0, "null_count": 0},
        ],
    }]

    FIELDS = [
        {"name": "Sales", "tags": ["$numeric"], "is_numeric": True, "cardinality": 193},
        {"name": "Region", "tags": ["$text"], "is_numeric": False, "cardinality": 5},
    ]

    def test_merges_tags_into_table_fields(self):
        result = describe_data_model(FakeModelEngine(self.TABLES, self.FIELDS))
        fields = {f["name"]: f for f in result["tables"][0]["fields"]}
        assert fields["Sales"]["is_numeric"] is True
        assert fields["Region"]["tags"] == ["$text"]

    def test_fills_a_missing_distinct_count_from_the_field_list(self):
        result = describe_data_model(FakeModelEngine(self.TABLES, self.FIELDS))
        fields = {f["name"]: f for f in result["tables"][0]["fields"]}
        assert fields["Sales"]["distinct"] == 193

    def test_does_not_overwrite_a_known_distinct_count(self):
        result = describe_data_model(FakeModelEngine(self.TABLES, self.FIELDS))
        fields = {f["name"]: f for f in result["tables"][0]["fields"]}
        assert fields["Region"]["distinct"] == 5

    def test_includes_quality_findings(self):
        tables = [{"name": "Empty", "rows": 0, "fields": []}]
        result = describe_data_model(FakeModelEngine(tables))
        assert result["quality_counts"]["high"] == 1

    def test_survives_a_failing_field_list(self):
        """Field metadata is a bonus; the table picture still comes back."""
        engine = FakeModelEngine(self.TABLES, fields_error=RuntimeError("boom"))
        result = describe_data_model(engine)
        assert result["total_rows"] == 100

    def test_reports_total_rows(self):
        result = describe_data_model(FakeModelEngine(self.TABLES, self.FIELDS))
        assert result["total_rows"] == 100


class TestReloadProgress:
    """A failed reload has to explain itself, or the script can't be fixed."""

    def test_collects_errors_and_messages(self, engine):
        engine.ws.handlers = {"GetProgress": {"qProgressData": {
            "qErrorData": [{"qMessageParameters": ["Field not found: 'Sales'"]}],
            "qPersistentProgressMessages": [
                {"qMessageParameters": ["Orders", "180,519 lines fetched"]}
            ],
        }}}

        progress = engine.get_reload_progress()

        assert progress["errors"] == ["Field not found: 'Sales'"]
        assert progress["messages"] == ["Orders 180,519 lines fetched"]

    def test_blank_message_parameters_are_dropped(self, engine):
        engine.ws.handlers = {"GetProgress": {"qProgressData": {
            "qErrorData": [],
            "qPersistentProgressMessages": [{"qMessageParameters": [""]}],
        }}}
        assert engine.get_reload_progress()["messages"] == []

    def test_missing_progress_support_is_not_fatal(self, engine):
        engine.ws.handlers = {"GetProgress": {"error": {"message": "unsupported"}}}
        assert engine.get_reload_progress() == {"messages": [], "errors": []}

    def test_reload_attaches_errors_to_the_result(self, engine):
        engine.ws.handlers = {
            "DoReloadEx": {"qResult": {"qSuccess": False, "qScriptLogFile": "C:/log.txt"}},
            "GetProgress": {"qProgressData": {
                "qErrorData": [{"qMessageParameters": ["Syntax error at line 3"]}],
                "qPersistentProgressMessages": [],
            }},
        }

        result = engine.reload_data()

        assert result["success"] is False
        assert result["errors"] == ["Syntax error at line 3"]


class TestSetScriptSafety:
    """Overwriting a load script is destructive and easy to do by accident."""

    def test_returns_the_previous_script_for_undo(self, engine):
        engine.ws.handlers = {
            "GetScript": {"qScript": "OLD SCRIPT"},
            "SetScript": {},
            "CheckScriptSyntax": {"qErrors": []},
        }
        assert engine.set_script("NEW SCRIPT") == "OLD SCRIPT"

    def test_a_broken_script_is_rolled_back(self, engine):
        engine.ws.handlers = {
            "GetScript": {"qScript": "GOOD SCRIPT"},
            "SetScript": {},
            "CheckScriptSyntax": {"qErrors": [{"qLineInTab": 3, "qTabIx": 0}]},
        }

        with pytest.raises(Exception, match="syntax error"):
            engine.set_script("BROKEN (((")

        # Last write must put the original back, not leave the broken one.
        assert engine.ws.requests_for("SetScript")[-1]["params"] == ["GOOD SCRIPT"]

    def test_validation_can_be_skipped_for_a_known_good_restore(self, engine):
        engine.ws.handlers = {"GetScript": {"qScript": "OLD"}, "SetScript": {}}
        engine.set_script("RESTORED", validate=False)
        assert engine.ws.requests_for("CheckScriptSyntax") == []
