import pytest

from data_prep import generate_load_script, qlik_quote, split_tabs

SALES = {
    "connection": "dataset", "path": "sales.csv", "table": "Sales",
    "columns": ["Order Id", "Region", "Amount"],
}
RETURNS = {
    "connection": "dataset", "path": "returns.csv", "table": "Returns",
    "columns": ["Order Id", "Region", "Amount"],
}
DOWNLOADS = {
    "connection": "downloads", "path": "extra.csv", "table": "Extra",
    "columns": ["Order Id", "Notes"],
}


def script_for(sources, **kwargs):
    return generate_load_script(sources, **kwargs)["script"]


class TestQuoting:

    def test_brackets_a_name(self):
        assert qlik_quote("Order Id") == "[Order Id]"

    def test_escapes_a_closing_bracket(self):
        assert qlik_quote("weird]name") == "[weird]]name]"


class TestSingleSource:

    def test_names_the_table(self):
        assert "[Sales]:" in script_for([SALES])

    def test_lists_every_column(self):
        script = script_for([SALES], trim_text=False)
        for column in SALES["columns"]:
            assert qlik_quote(column) in script

    def test_references_the_lib_path(self):
        assert "[lib://dataset/sales.csv]" in script_for([SALES])

    def test_a_bracket_in_the_path_is_escaped(self):
        source = dict(SALES, path="sales]v2.csv")
        assert "FROM [lib://dataset/sales]]v2.csv]" in script_for([source])

    def test_has_a_terminated_from_clause(self):
        script = script_for([SALES])
        assert "embedded labels" in script
        assert script.rstrip().endswith(";")


class TestCleaning:

    def test_trims_text_by_default(self):
        script = script_for([SALES])
        assert "Trim([Region]) as [Region]" in script

    def test_trim_can_be_turned_off(self):
        assert "Trim(" not in script_for([SALES], trim_text=False)

    def test_null_tokens_become_real_nulls(self):
        script = script_for([SALES], trim_text=False, null_tokens=["N/A"])
        assert "If([Region] = 'N/A', Null(), [Region]) as [Region]" in script

    def test_multiple_null_tokens_nest(self):
        script = script_for([SALES], trim_text=False, null_tokens=["N/A", "-"])
        assert "'N/A'" in script and "'-'" in script

    def test_a_null_token_with_an_apostrophe_is_escaped(self):
        script = script_for([SALES], trim_text=False, null_tokens=["n'a"])
        assert "'n''a'" in script
        assert "'n'a'" not in script

    def test_drop_fields_removes_a_column(self):
        script = script_for([SALES], drop_fields=["Amount"])
        assert "[Amount]" not in script
        assert "[Region]" in script

    def test_drop_fields_is_case_insensitive(self):
        assert "[Amount]" not in script_for([SALES], drop_fields=["amount"])

    def test_dropping_everything_is_reported_not_crashed(self):
        result = generate_load_script([SALES], drop_fields=SALES["columns"])
        assert result["script"].strip() == ""
        assert any("skipping" in n for n in result["notes"])

    def test_notes_mention_the_dropped_count(self):
        notes = generate_load_script([SALES], drop_fields=["Amount"])["notes"]
        assert any("dropping 1 column" in n for n in notes)


class TestNumericColumnsAreNotTrimmed:
    TYPED = {
        "connection": "dataset", "path": "sales.csv", "table": "Sales",
        "columns": ["Region", "Amount", "Quantity"],
        "sample_rows": [
            {"Region": " North ", "Amount": "314.64", "Quantity": "3"},
            {"Region": "South", "Amount": "-249.09", "Quantity": "5"},
        ],
    }

    def test_text_column_is_trimmed(self):
        assert "Trim([Region]) as [Region]" in script_for([self.TYPED])

    @pytest.mark.parametrize("column", ["Amount", "Quantity"])
    def test_numeric_columns_are_not_trimmed(self, column):
        assert f"Trim([{column}])" not in script_for([self.TYPED])

    def test_untouched_numeric_columns_are_plain_references(self):
        script = script_for([self.TYPED])
        assert "    [Amount]," in script or "    [Amount]\n" in script

    def test_negative_and_decimal_values_still_count_as_numeric(self):
        assert "Trim([Amount])" not in script_for([self.TYPED])

    def test_null_token_wrapping_preserves_a_numeric_column(self):
        script = script_for([self.TYPED], null_tokens=["N/A"])
        assert "If([Amount] = 'N/A', Null(), [Amount]) as [Amount]" in script

    def test_notes_mention_the_numeric_columns(self):
        notes = generate_load_script([self.TYPED])["notes"]
        assert any("numeric column" in n for n in notes)

    def test_without_samples_everything_is_treated_as_text(self):
        source = dict(self.TYPED)
        source.pop("sample_rows")
        assert "Trim([Amount])" in script_for([source])

    def test_a_mixed_column_is_treated_as_text(self):
        source = dict(self.TYPED)
        source["sample_rows"] = [{"Amount": "314.64"}, {"Amount": "unknown"}]
        source["columns"] = ["Amount"]
        assert "Trim([Amount])" in script_for([source])

    def test_blank_values_do_not_make_a_column_text(self):
        source = dict(self.TYPED)
        source["columns"] = ["Amount"]
        source["sample_rows"] = [{"Amount": "1.5"}, {"Amount": ""}, {"Amount": "2"}]
        assert "Trim([Amount])" not in script_for([source])

    def test_an_all_blank_column_is_treated_as_text(self):
        source = dict(self.TYPED)
        source["columns"] = ["Amount"]
        source["sample_rows"] = [{"Amount": ""}, {"Amount": ""}]
        assert "Trim([Amount])" in script_for([source])

    @pytest.mark.parametrize("value", ["nan", "inf", "-Infinity", "1_000"])
    def test_pythonisms_qlik_reads_as_text_are_treated_as_text(self, value):
        source = dict(self.TYPED)
        source["columns"] = ["Amount"]
        source["sample_rows"] = [{"Amount": "1.5"}, {"Amount": value}]
        assert "Trim([Amount])" in script_for([source])


class TestConcatenate:
    def test_first_source_declares_the_table(self):
        script = script_for([SALES, RETURNS], mode="concatenate")
        assert "[Sales]:" in script

    def test_later_sources_concatenate_into_it(self):
        script = script_for([SALES, RETURNS], mode="concatenate")
        assert "CONCATENATE ([Sales])" in script

    def test_names_the_concatenate_target_explicitly(self):
        script = script_for([SALES, RETURNS], mode="concatenate")
        assert "CONCATENATE\n" not in script

    def test_table_name_can_be_overridden(self):
        script = script_for([SALES, RETURNS], mode="concatenate", table_name="AllOrders")
        assert "[AllOrders]:" in script
        assert "CONCATENATE ([AllOrders])" in script

    def test_a_nameless_first_source_gets_a_real_default_not_none(self):
        nameless = {k: v for k, v in SALES.items() if k != "table"}
        script = script_for([nameless, RETURNS], mode="concatenate")
        assert "[None]" not in script
        assert "[Data]:" in script
        assert "CONCATENATE ([Data])" in script

    def test_both_files_are_loaded(self):
        script = script_for([SALES, RETURNS], mode="concatenate")
        assert "sales.csv" in script and "returns.csv" in script

    def test_warns_that_columns_should_match(self):
        notes = generate_load_script([SALES, DOWNLOADS], mode="concatenate")["notes"]
        assert any("share column names" in n for n in notes)

    def test_sources_can_span_connections(self):
        script = script_for([SALES, DOWNLOADS], mode="concatenate")
        assert "[lib://dataset/sales.csv]" in script
        assert "[lib://downloads/extra.csv]" in script


class TestSeparate:

    def test_each_source_gets_its_own_table(self):
        script = script_for([SALES, RETURNS])
        assert "[Sales]:" in script and "[Returns]:" in script

    def test_does_not_concatenate(self):
        assert "CONCATENATE" not in script_for([SALES, RETURNS])

    def test_falls_back_to_a_positional_name(self):
        source = dict(SALES)
        source["table"] = ""
        assert "[Table1]:" in script_for([source])


class TestValidation:

    def test_rejects_an_unknown_mode(self):
        with pytest.raises(ValueError, match="separate"):
            generate_load_script([SALES], mode="merge")

    def test_no_sources_is_an_error_not_a_silent_empty_script(self):
        with pytest.raises(ValueError, match="sources"):
            generate_load_script([])


class TestScriptIsUsableAsATab:
    def test_round_trips_through_tab_handling(self):
        from data_prep import get_tab, set_tab

        generated = script_for([SALES, RETURNS], mode="concatenate")
        script = set_tab("///$tab Main\r\nLOAD 1;\r\n", generated, tab_name="Loader")

        assert [n for n, _ in split_tabs(script)] == ["Main", "Loader"]
        assert "CONCATENATE ([Sales])" in get_tab(script, "Loader")
        assert "LOAD 1;" in get_tab(script, "Main")
