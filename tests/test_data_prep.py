"""Data quality analysis and load-script tab handling."""

import pytest

from data_prep import (
    CRITICAL_DENSITY,
    GENERATED_TAB,
    SPARSE_DENSITY,
    analyse_tables,
    join_tabs,
    lib_path,
    quality_report,
    replace_tab,
    split_tabs,
)


def field(name, density=1.0, distinct=10, rows=1000):
    non_nulls = int(rows * density)
    return {
        "name": name, "rows": rows, "non_nulls": non_nulls,
        "null_count": rows - non_nulls, "density": density,
        "distinct": distinct, "has_duplicates": False, "is_key": False,
    }


def table(name="Orders", rows=1000, fields=None):
    return {"name": name, "rows": rows, "fields": fields or []}


class TestQualityFindings:

    def test_clean_data_produces_no_findings(self):
        assert analyse_tables([table(fields=[field("Sales"), field("Region")])]) == []

    def test_flags_an_empty_table(self):
        findings = analyse_tables([table(rows=0)])
        assert findings[0]["severity"] == "high"
        assert findings[0]["issue"] == "empty table"

    def test_flags_an_almost_entirely_null_column(self):
        findings = analyse_tables([table(fields=[field("Notes", density=0.02)])])
        assert findings[0]["severity"] == "high"
        assert "null" in findings[0]["issue"]

    def test_flags_a_sparse_column_as_medium(self):
        findings = analyse_tables([table(fields=[field("Notes", density=0.3)])])
        assert findings[0]["severity"] == "medium"
        assert findings[0]["issue"] == "sparse"
        assert "700" in findings[0]["detail"]  # 70% of 1000 rows null

    def test_sparse_suggestion_names_the_field(self):
        findings = analyse_tables([table(fields=[field("Notes", density=0.3)])])
        assert "[Notes]" in findings[0]["suggestion"]

    def test_flags_a_constant_column(self):
        findings = analyse_tables([table(fields=[field("Country", distinct=1)])])
        assert any(f["issue"] == "constant" for f in findings)

    def test_a_healthy_column_is_not_flagged(self):
        assert analyse_tables([table(fields=[field("Region", density=1.0, distinct=5)])]) == []

    @pytest.mark.parametrize("density", [0.51, 0.75, 1.0])
    def test_density_above_the_threshold_is_fine(self, density):
        assert analyse_tables([table(fields=[field("X", density=density)])]) == []

    def test_high_severity_sorts_first(self):
        findings = analyse_tables([table(fields=[
            field("Sparse", density=0.3),
            field("Empty", density=0.01),
        ])])
        assert [f["severity"] for f in findings] == ["high", "medium"]

    def test_thresholds_are_sane(self):
        assert 0 < CRITICAL_DENSITY < SPARSE_DENSITY < 1

    def test_a_field_with_unknown_density_is_skipped(self):
        f = field("X")
        f["density"] = None
        assert analyse_tables([table(fields=[f])]) == []


class TestQualityReport:

    def test_summarises_tables_and_counts(self):
        report = quality_report([
            table("Orders", 1000, [field("Sales"), field("Notes", density=0.02)]),
            table("Customers", 50, [field("Name")]),
        ])

        assert report["total_rows"] == 1050
        assert report["counts"]["high"] == 1
        assert {t["name"] for t in report["tables"]} == {"Orders", "Customers"}


class TestScriptTabs:

    SCRIPT = (
        "///$tab Main\r\nLOAD * FROM [lib://data/x.csv];\r\n"
        "///$tab other\r\nLIB CONNECT TO 'rest';\r\n"
    )

    def test_splits_named_tabs(self):
        assert [name for name, _ in split_tabs(self.SCRIPT)] == ["Main", "other"]

    def test_split_keeps_bodies(self):
        tabs = dict(split_tabs(self.SCRIPT))
        assert "LOAD * FROM" in tabs["Main"]
        assert "LIB CONNECT" in tabs["other"]

    def test_an_unmarked_script_is_one_tab(self):
        assert split_tabs("LOAD * FROM [x];") == [("Main", "LOAD * FROM [x];")]

    def test_round_trips(self):
        assert split_tabs(join_tabs(split_tabs(self.SCRIPT))) == split_tabs(self.SCRIPT)

    def test_appends_a_generated_tab(self):
        result = replace_tab(self.SCRIPT, "LOAD 1 AS x AUTOGENERATE 1;")
        names = [name for name, _ in split_tabs(result)]
        assert names == ["Main", "other", GENERATED_TAB]

    def test_preserves_hand_written_tabs(self):
        """The whole point of writing into its own tab."""
        result = replace_tab(self.SCRIPT, "LOAD 1 AS x AUTOGENERATE 1;")
        assert "LIB CONNECT TO 'rest';" in result
        assert "LOAD * FROM [lib://data/x.csv];" in result

    def test_regenerating_replaces_rather_than_appends(self):
        once = replace_tab(self.SCRIPT, "LOAD 1 AS a AUTOGENERATE 1;")
        twice = replace_tab(once, "LOAD 2 AS b AUTOGENERATE 1;")

        names = [name for name, _ in split_tabs(twice)]
        assert names.count(GENERATED_TAB) == 1
        assert "AS b" in twice and "AS a" not in twice

    def test_tab_match_is_case_insensitive(self):
        once = replace_tab(self.SCRIPT, "LOAD 1 AS a AUTOGENERATE 1;", tab_name="Gen")
        twice = replace_tab(once, "LOAD 2 AS b AUTOGENERATE 1;", tab_name="gen")
        assert [n for n, _ in split_tabs(twice)].count("Gen") == 1


class TestLibPath:

    @pytest.mark.parametrize("connection,path,expected", [
        ("dataset", "sales.csv", "lib://dataset/sales.csv"),
        ("dataset", "/sales.csv", "lib://dataset/sales.csv"),
        ("dataset", "sub/sales.csv", "lib://dataset/sub/sales.csv"),
        ("dataset", "", "lib://dataset"),
    ])
    def test_builds_a_lib_reference(self, connection, path, expected):
        assert lib_path(connection, path) == expected
