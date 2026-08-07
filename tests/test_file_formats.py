"""Non-CSV files must be previewed and loaded as what they are.

Every preview used CSV_FORMAT and every generated FROM clause said
`(txt, ...)` whatever the file was - so an .xlsx or .qvd "preview" returned
binary garbage as column names, and the script generated from it could not
run. These pin the format to the extension at both ends.
"""

from chat_tools import build_load_script
from data_prep import from_format_spec, generate_load_script
from qlik_engine import file_format_for


class TestPreviewFormat:

    def test_csv_stays_csv(self):
        assert file_format_for("sales.csv")["qType"] == "CSV"

    def test_case_and_path_do_not_matter(self):
        assert file_format_for("folder/Sales.XLSX")["qType"] == "EXCEL_OOXML"

    def test_excel(self):
        assert file_format_for("sales.xlsx")["qType"] == "EXCEL_OOXML"
        assert file_format_for("sales.xls")["qType"] == "EXCEL_BIFF"

    def test_qvd_json_parquet(self):
        assert file_format_for("x.qvd")["qType"] == "QVD"
        assert file_format_for("x.json")["qType"] == "JSON"
        assert file_format_for("x.parquet")["qType"] == "PARQUET"

    def test_tab_file_gets_a_tab_delimiter(self):
        file_format = file_format_for("x.tab")
        assert file_format["qType"] == "CSV"
        assert file_format["qDelimiter"]["qNumber"] == 9

    def test_unknown_extension_falls_back_to_csv(self):
        assert file_format_for("mystery.dat")["qType"] == "CSV"

    def test_every_field_the_engine_requires_is_present(self):
        """The engine rejects a partial FileDataFormat outright."""
        csv_keys = set(file_format_for("a.csv"))
        assert set(file_format_for("a.xlsx")) == csv_keys


class TestFromClauseFormat:

    def test_csv(self):
        assert from_format_spec("sales.csv") == (
            "(txt, utf8, embedded labels, delimiter is ',', msq)"
        )

    def test_xlsx_names_the_sheet(self):
        assert from_format_spec("sales.xlsx", "Sheet1") == (
            "(ooxml, embedded labels, table is [Sheet1])"
        )

    def test_xlsx_without_a_sheet_name_still_parses(self):
        assert from_format_spec("sales.xlsx") == "(ooxml, embedded labels)"

    def test_xls_gets_the_biff_dollar(self):
        assert from_format_spec("old.xls", "Sheet1") == (
            "(biff, embedded labels, table is [Sheet1$])"
        )

    def test_qvd_and_parquet(self):
        assert from_format_spec("x.qvd") == "(qvd)"
        assert from_format_spec("x.parquet") == "(parquet)"

    def test_tab_delimited(self):
        assert "delimiter is '\\t'" in from_format_spec("x.tab")


class TestGeneratedScriptUsesTheRightFormat:

    def test_excel_source_loads_as_ooxml(self):
        result = generate_load_script([{
            "connection": "data", "path": "report.xlsx",
            "table": "Report", "file_table": "Sheet1",
            "columns": ["Region", "Amount"],
        }])
        assert "(ooxml, embedded labels, table is [Sheet1]);" in result["script"]
        assert "txt, utf8" not in result["script"]

    def test_csv_source_is_unchanged(self):
        result = generate_load_script([{
            "connection": "data", "path": "sales.csv",
            "table": "Sales", "columns": ["Region", "Amount"],
        }])
        assert "(txt, utf8, embedded labels, delimiter is ',', msq);" in result["script"]


class FakeEngine:
    """Just enough engine to preview one Excel file."""

    def preview_file(self, connection, path, sample_rows=5):
        return {
            "path": path,
            "tables": [{
                "name": "Sheet1",
                "columns": ["Region", "Amount"],
                "sample_rows": [{"Region": "West", "Amount": "10"}],
            }],
        }


class TestBuildLoadScriptCarriesTheSheetName:

    def test_the_in_file_table_reaches_the_from_clause(self):
        result = build_load_script(
            FakeEngine(),
            sources=[{"connection": "data", "path": "report.xlsx"}],
        )
        assert "table is [Sheet1]" in result["script"]

    def test_renaming_the_target_keeps_the_sheet_name(self):
        """`table` renames the loaded table; the format spec still needs the
        sheet's own name."""
        result = build_load_script(
            FakeEngine(),
            sources=[{"connection": "data", "path": "report.xlsx", "table": "Facts"}],
        )
        assert "[Facts]:" in result["script"]
        assert "table is [Sheet1]" in result["script"]
