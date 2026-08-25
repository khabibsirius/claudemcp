"""Calculated columns.

"Add a Year column from Report Date" had no answer: the generator could only
copy the file's own columns, and the assistant was told never to hand-write a
LOAD - so it refused, repeatedly, saying it was "not permitted". The rule was
right; the missing capability was the bug.
"""

import pytest

from data_prep import generate_load_script, normalise_derived

SOURCE = [{
    "connection": "Videos2",
    "path": "download.csv",
    "table": "Downloads",
    "columns": ["Report Date", "agent", "Region", "Currency", "deposit_type", "SUM"],
    "sample_rows": [{
        "Report Date": "01.01.2026", "agent": "a", "Region": "Almaty",
        "Currency": "KZT", "deposit_type": "term", "SUM": "1500",
    }],
}]


def script(**kwargs):
    return generate_load_script(SOURCE, **kwargs)["script"]


class TestNormaliseDerived:

    def test_accepts_dicts(self):
        assert normalise_derived([{"name": "Year", "expression": "Year([D])"}]) == [
            ("Year", "Year([D])")
        ]

    def test_accepts_pairs(self):
        assert normalise_derived([("Year", "Year([D])")]) == [("Year", "Year([D])")]

    @pytest.mark.parametrize("entry", [
        {"name": "", "expression": "Year([D])"},
        {"name": "Year", "expression": ""},
        {},
    ])
    def test_incomplete_entries_are_dropped(self, entry):
        """`None as [None]` would fail the syntax check and take the whole
        script down with it."""
        assert normalise_derived([entry]) == []

    def test_a_trailing_alias_is_removed(self):
        """Models often include the "as X" the generator is about to add."""
        assert normalise_derived([
            {"name": "Year", "expression": "Year([Report Date]) as Year"}
        ]) == [("Year", "Year([Report Date])")]

    def test_a_bracketed_trailing_alias_is_removed(self):
        assert normalise_derived([
            {"name": "Y", "expression": "Year([D]) as [My Year]"}
        ]) == [("Y", "Year([D])")]

    def test_an_as_inside_a_string_literal_is_not_an_alias(self):
        """The stripper once ate the tail of If(a, 'x as y'), leaving
        If(a, 'x - a broken literal that failed the syntax check."""
        expression = "If([Status] = 1, 'marked as new', 'old')"
        assert normalise_derived([{"name": "Flag", "expression": expression}]) == [
            ("Flag", expression)
        ]

    def test_an_alias_after_a_string_literal_is_still_removed(self):
        assert normalise_derived([
            {"name": "Flag", "expression": "If([A] = 1, 'x as y', 'z') as Flag"}
        ]) == [("Flag", "If([A] = 1, 'x as y', 'z')")]

    def test_nothing_in_nothing_out(self):
        assert normalise_derived(None) == []


class TestGeneratedScript:

    def test_a_derived_column_appears(self):
        text = script(derived=[{"name": "Year", "expression": "Year([Report Date])"}])
        assert "Year([Report Date]) as [Year]" in text

    def test_the_name_is_bracket_quoted(self):
        """Names with spaces are the normal case in Qlik."""
        text = script(derived=[{"name": "High Deposit", "expression": "If([SUM]>100,1,0)"}])
        assert "as [High Deposit]" in text

    def test_original_columns_survive(self):
        text = script(derived=[{"name": "Year", "expression": "Year([Report Date])"}])
        for column in ("Region", "Currency", "deposit_type"):
            assert column in text

    def test_derived_columns_come_last(self):
        """After the file's own fields, so an expression can refer to them."""
        text = script(derived=[{"name": "Year", "expression": "Year([Report Date])"}])
        assert text.index("[Region]") < text.index("as [Year]")

    def test_several_at_once(self):
        text = script(derived=[
            {"name": "Year", "expression": "Year([Report Date])"},
            {"name": "Month", "expression": "Month([Report Date])"},
            {"name": "HighDeposit", "expression": "If([SUM] > 100, 1, 0)"},
        ])
        for name in ("[Year]", "[Month]", "[HighDeposit]"):
            assert f"as {name}" in text

    def test_the_field_list_stays_comma_separated(self):
        """A missing comma between the last real field and the first derived
        one is a syntax error."""
        text = script(derived=[{"name": "Year", "expression": "Year([Report Date])"}])
        body = text[text.index("LOAD"):text.index("FROM")]
        assert ",\n    Year([Report Date]) as [Year]" in body

    def test_the_statement_still_terminates(self):
        text = script(derived=[{"name": "Year", "expression": "Year([Report Date])"}])
        assert text.rstrip().endswith(";")

    def test_no_derived_columns_changes_nothing(self):
        assert script() == script(derived=[])

    def test_they_are_reported_in_the_notes(self):
        result = generate_load_script(
            SOURCE, derived=[{"name": "Year", "expression": "Year([Report Date])"}]
        )
        assert any("Year = Year([Report Date])" in n for n in result["notes"])

    def test_derived_columns_survive_dropping_others(self):
        text = script(
            drop_fields=["Currency"],
            derived=[{"name": "Year", "expression": "Year([Report Date])"}],
        )
        assert "as [Year]" in text
        assert "[Currency]" not in text

    def test_numeric_columns_are_still_left_untrimmed(self):
        """Trimming turns a number into text and Sum() over it stops working."""
        text = script(derived=[{"name": "Year", "expression": "Year([Report Date])"}])
        assert "Trim([SUM])" not in text


class TestToolExposesIt:

    def test_the_schema_offers_derived(self):
        from chat_tools import TOOLS

        tool = next(t for t in TOOLS if t["function"]["name"] == "build_load_script")
        assert "derived" in tool["function"]["parameters"]["properties"]

    def test_the_description_says_it_can_add_columns(self):
        from chat_tools import TOOLS

        tool = next(t for t in TOOLS if t["function"]["name"] == "build_load_script")
        assert "ADD NEW COLUMNS" in tool["function"]["description"]

    def test_the_prompt_no_longer_forbids_expressions(self):
        """It refused four times saying it was "not permitted"."""
        from chat_tools import SYSTEM_PROMPT

        assert "never tell the user that adding a calculated field is impossible" in SYSTEM_PROMPT
        assert "TO ADD A NEW OR CALCULATED COLUMN" in SYSTEM_PROMPT


class TestAliasesInAnyLanguage:
    """The alias stripper matched ASCII identifiers only, so a Russian one
    was left in place and the generator added a second: the script came out
    as "Year([Report Date]) as Год as [Год]" and failed the syntax check.
    Field names in this app are as often Cyrillic as not."""

    @pytest.mark.parametrize("alias", ["Year", "Год", "Yil", "год_отчёта"])
    def test_an_alias_is_stripped_whatever_it_is_written_in(self, alias):
        cleaned = normalise_derived(
            [{"name": alias, "expression": f"Year([Report Date]) as {alias}"}]
        )
        assert cleaned == [(alias, "Year([Report Date])")]

    def test_a_bracketed_alias_is_still_stripped(self):
        cleaned = normalise_derived(
            [{"name": "Год", "expression": "Year([Report Date]) as [Год]"}]
        )
        assert cleaned == [("Год", "Year([Report Date])")]
