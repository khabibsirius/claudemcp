"""Facts read out of a sheet, and the arithmetic behind them.

The point of insights.py is that the model never does this sum. So these
tests are the only thing standing between a banking user and a confidently
worded percentage that is wrong - which is worse than no percentage, because
they have no way to check it.
"""

import insights
from insights import analyse_chart, analyse_sheet


class FakeEngine:
    """Answers queries from a canned table, and records what was asked.

    The recording matters: a trend read off a measure-sorted series is not a
    trend, so the sort flag the caller passed is part of what is under test.
    """

    def __init__(self, charts=None, rows=None, fields=None, total_rows=None):
        self.charts = charts or []
        self.rows = rows or {}
        self.fields = fields or []
        self.total_rows = total_rows
        self.queries = []

    def list_charts(self):
        return self.charts

    def get_fields(self):
        return self.fields

    def query(self, dimensions=None, measures=None, limit=50, sort_by_measure=True):
        key = (dimensions or [None])[0]
        self.queries.append(
            {"dimension": key, "measure": (measures or [None])[0],
             "sort_by_measure": sort_by_measure, "limit": limit}
        )
        rows = self.rows.get(key, [])
        dimension_columns = [d for d in (dimensions or [])]
        columns = dimension_columns + ["value"]
        return {
            "columns": columns,
            "rows": rows,
            "returned_rows": len(rows),
            "total_rows": self.total_rows if self.total_rows is not None else len(rows),
        }


def chart(title="Chart", type="barchart", dimensions=("Region",),
          measures=("Sum([SUM])",), sheet="Sheet 1", id="c1"):
    return {
        "id": id, "title": title, "type": type,
        "dimensions": list(dimensions), "measures": list(measures), "sheet": sheet,
    }


def rows_for(dimension, pairs):
    return [{dimension: label, "value": value} for label, value in pairs]


REGIONS = [("Almaty", 80.0), ("Astana", 60.0), ("Karaganda", 40.0), ("Turkistan", 20.0)]


# -- the sums themselves ------------------------------------------------

def test_share_and_total_come_from_the_rows():
    engine = FakeEngine(rows={"Region": rows_for("Region", REGIONS)})
    facts = analyse_chart(engine, chart())["facts"]

    assert facts["total"] == 200.0
    assert facts["categories"] == 4
    assert facts["highest"] == {"label": "Almaty", "value": 80.0, "share_pct": 40.0}
    assert facts["lowest"] == {"label": "Turkistan", "value": 20.0}


def test_concentration_is_the_top_three_share():
    engine = FakeEngine(rows={"Region": rows_for("Region", REGIONS)})
    facts = analyse_chart(engine, chart())["facts"]

    assert facts["concentration"]["share_pct"] == 90.0
    assert facts["concentration"]["labels"] == ["Almaty", "Astana", "Karaganda"]


def test_ranking_does_not_assume_the_query_came_back_sorted():
    """The engine sorts, but a scrambled page must not produce a wrong top."""
    scrambled = [("Astana", 60.0), ("Turkistan", 20.0), ("Almaty", 80.0), ("Karaganda", 40.0)]
    engine = FakeEngine(rows={"Region": rows_for("Region", scrambled)})
    facts = analyse_chart(engine, chart())["facts"]

    assert facts["highest"]["label"] == "Almaty"
    assert facts["lowest"]["label"] == "Turkistan"


def test_fewer_than_four_categories_has_no_concentration():
    """"The top 3 of 3 hold 100%" is arithmetic, not a finding."""
    engine = FakeEngine(rows={"Region": rows_for("Region", REGIONS[:3])})
    assert "concentration" not in analyse_chart(engine, chart())["facts"]


# -- the guard that stops a meaningless percentage ----------------------

def test_an_average_gets_no_total_and_no_share():
    """Averaging averages is not the average, and 37% of a column of them
    means nothing. A financial reader spots it immediately."""
    engine = FakeEngine(rows={"Region": rows_for("Region", REGIONS)})
    facts = analyse_chart(engine, chart(measures=("Avg([SUM])",)))["facts"]

    assert "total" not in facts
    assert "concentration" not in facts
    assert "share_pct" not in facts["highest"]
    assert "measure_is_not_additive" in facts


def test_count_and_sum_are_both_additive():
    for expression in ("Sum([SUM])", "=Sum([SUM])", "Count(DISTINCT [Id])", "  count([Id])"):
        assert insights._is_additive(expression), expression
    for expression in ("Avg([SUM])", "Min([SUM])", "Sum([A])/Sum([B])", "", None):
        assert not insights._is_additive(expression), expression


# -- time ---------------------------------------------------------------

MONTHS = [("2026-01", 100.0), ("2026-02", 120.0), ("2026-03", 90.0), ("2026-04", 150.0)]


def test_a_time_dimension_is_read_in_its_own_order_not_by_size():
    engine = FakeEngine(rows={"Report Date": rows_for("Report Date", MONTHS)})
    analyse_chart(engine, chart(dimensions=("Report Date",)))

    assert engine.queries[0]["sort_by_measure"] is False


def test_a_categorical_dimension_is_read_largest_first():
    engine = FakeEngine(rows={"Region": rows_for("Region", REGIONS)})
    analyse_chart(engine, chart())

    assert engine.queries[0]["sort_by_measure"] is True


def test_trend_measures_first_to_last_and_the_latest_step():
    engine = FakeEngine(rows={"Report Date": rows_for("Report Date", MONTHS)})
    trend = analyse_chart(engine, chart(dimensions=("Report Date",)))["facts"]["trend"]

    assert trend["first"]["label"] == "2026-01"
    assert trend["last"]["label"] == "2026-04"
    assert trend["change"] == 50.0
    assert trend["change_pct"] == 50.0
    assert trend["direction"] == "up"
    assert trend["peak"]["label"] == "2026-04"
    assert trend["trough"]["label"] == "2026-03"
    # The step into the newest period is invisible in a start-to-end change.
    assert trend["latest_step"] == {
        "from": "2026-03", "to": "2026-04", "change": 60.0, "change_pct": 66.7,
    }


def test_a_falling_series_reads_as_down():
    falling = [("2026-01", 200.0), ("2026-02", 150.0), ("2026-03", 100.0)]
    engine = FakeEngine(rows={"Report Date": rows_for("Report Date", falling)})
    trend = analyse_chart(engine, chart(dimensions=("Report Date",)))["facts"]["trend"]

    assert trend["direction"] == "down"
    assert trend["change"] == -100.0
    assert trend["change_pct"] == -50.0


def test_no_concentration_on_a_date_axis():
    """"The top 3 of 6 months hold 50%" is not a finding about a deposit book."""
    engine = FakeEngine(rows={"Report Date": rows_for("Report Date", MONTHS)})
    facts = analyse_chart(engine, chart(dimensions=("Report Date",)))["facts"]

    assert "concentration" not in facts
    assert "trend" in facts


def test_a_date_is_recognised_by_qlik_tag_not_only_by_its_name():
    engine = FakeEngine(rows={"Period": rows_for("Period", MONTHS)})
    described = analyse_chart(engine, chart(dimensions=("Period",)), {"Period": ["$date"]})

    assert "trend" in described["facts"]


def test_a_number_called_period_is_still_a_date_by_name():
    """The name heuristic is deliberately generous; being wrong here costs a
    trend line on a categorical axis, not a wrong number."""
    assert insights._is_time_field("Reporting Period", [])
    assert not insights._is_time_field("Region", [])
    assert not insights._is_time_field("Currency", ["$ascii"])


# -- charts that cannot be summarised -----------------------------------

def test_a_kpi_reports_its_single_value():
    engine = FakeEngine(rows={None: [{"value": 207.5}]})
    described = analyse_chart(engine, chart(type="kpi", dimensions=()))

    assert described["facts"] == {"value": 207.5}


def test_a_filterpane_is_skipped_rather_than_queried():
    engine = FakeEngine()
    described = analyse_chart(engine, chart(type="filterpane", measures=()))

    assert "facts" not in described
    assert described["skipped"]
    assert engine.queries == []


def test_a_chart_that_cannot_be_read_does_not_fail_the_turn():
    class Broken(FakeEngine):
        def query(self, **kwargs):
            raise RuntimeError("field not found")

    described = analyse_chart(Broken(), chart())
    assert "could not be read" in described["skipped"]


def test_text_values_are_not_counted_as_numbers():
    engine = FakeEngine(rows={"Region": [{"Region": "Almaty", "value": "-"}]})
    assert "facts" not in analyse_chart(engine, chart())


def test_a_multi_measure_chart_says_it_was_summarised_on_the_first():
    engine = FakeEngine(rows={"Region": rows_for("Region", REGIONS)})
    described = analyse_chart(
        engine, chart(measures=("Sum([SUM])", "Sum([Other])"))
    )
    assert described["note"]
    assert described["measure"] == "Sum([SUM])"


def test_truncation_is_reported_rather_than_hidden():
    engine = FakeEngine(rows={"Region": rows_for("Region", REGIONS)}, total_rows=900)
    facts = analyse_chart(engine, chart())["facts"]

    assert facts["truncated"] == {"read": 4, "of": 900}


# -- picking the sheet --------------------------------------------------

def test_a_named_sheet_is_matched_on_a_fragment():
    engine = FakeEngine(
        charts=[chart(sheet="Deposits Overview"), chart(sheet="Other", id="c2")],
        rows={"Region": rows_for("Region", REGIONS)},
    )
    assert analyse_sheet(engine, sheet="deposits")["sheet"] == "Deposits Overview"


def test_an_unknown_sheet_lists_what_there_is():
    engine = FakeEngine(charts=[chart(sheet="Deposits")])
    result = analyse_sheet(engine, sheet="Nope")

    assert "error" in result
    assert result["sheets"] == ["Deposits"]


def test_no_sheet_named_means_the_one_built_last():
    engine = FakeEngine(
        charts=[chart(sheet="Old", id="c1"), chart(sheet="Just Built", id="c2")],
        rows={"Region": rows_for("Region", REGIONS)},
    )
    assert analyse_sheet(engine)["sheet"] == "Just Built"


def test_specific_chart_ids_win_over_the_sheet():
    engine = FakeEngine(
        charts=[chart(id="c1", title="A"), chart(id="c2", title="B")],
        rows={"Region": rows_for("Region", REGIONS)},
    )
    result = analyse_sheet(engine, chart_ids=["c2"])

    assert [c["chart"] for c in result["charts"]] == ["B"]


def test_an_empty_app_says_so_instead_of_raising():
    assert "error" in analyse_sheet(FakeEngine())


def test_charts_on_no_sheet_are_not_summarised_as_one():
    """An object can exist in the app while being invisible in Qlik."""
    engine = FakeEngine(charts=[chart(sheet=None)])
    assert "error" in analyse_sheet(engine)


def test_field_metadata_failing_does_not_stop_the_reading():
    class NoFields(FakeEngine):
        def get_fields(self):
            raise RuntimeError("no app")

    engine = NoFields(
        charts=[chart()], rows={"Region": rows_for("Region", REGIONS)}
    )
    assert analyse_sheet(engine)["read"] == 1


# -- putting the app's own spelling back --------------------------------

LABELS = {"срочные и условные", "вклады до востребования", "г.Алматы", "Retail Banking"}


def test_a_paraphrased_label_is_restored():
    """A 26B model writing Uzbek prose about Russian categories mangles them,
    and the reader cannot tell a renamed category from a wrong number."""
    text = 'Eng katta ulush "срочные и условно-срок" turiga tegishli.'

    assert insights.snap_labels(text, LABELS) == (
        'Eng katta ulush "срочные и условные" turiga tegishli.'
    )


def test_a_bold_label_is_restored_too():
    assert "**г.Алматы**" in insights.snap_labels("Eng katta **г. Алматы** hudud.", LABELS)


def test_ordinary_prose_is_left_alone():
    text = 'The sheet shows "something else entirely" and **a heading**.'
    assert insights.snap_labels(text, LABELS) == text


def test_numbers_are_never_touched():
    text = 'Jami **207 186 004,3** va "90,8%".'
    assert insights.snap_labels(text, LABELS) == text


def test_a_correct_label_is_left_exactly_as_it_is():
    text = 'Eng kichik "вклады до востребования" turi.'
    assert insights.snap_labels(text, LABELS) == text


def test_a_genuinely_different_category_is_not_snapped():
    """Sharing a word is not being the same label."""
    text = 'The "Corporate Banking" segment grew.'
    assert insights.snap_labels(text, LABELS) == text


def test_nothing_to_snap_against_returns_the_text():
    assert insights.snap_labels("Anything at all.", set()) == "Anything at all."
    assert insights.snap_labels("", LABELS) == ""


def test_labels_are_collected_from_a_whole_analysis():
    analysis = {
        "sheet": "S",
        "charts": [
            {"chart": "A", "facts": {
                "highest": {"label": "г.Алматы", "value": 1},
                "lowest": {"label": "Область Ұлытау", "value": 2},
                "concentration": {"labels": ["г.Алматы", "г.Астана", "Караганда"]},
                "trend": {"peak": {"label": "01.06.2026", "value": 3}},
            }},
        ],
    }
    found = insights.labels_in(analysis)

    assert {"г.Алматы", "Область Ұлытау", "г.Астана", "Караганда", "01.06.2026"} <= found


def test_snapping_survives_a_label_that_is_a_regex_metacharacter():
    labels = {"Sum([SUM]) *special*"}
    text = 'The "Sum([SUM]) *specail*" measure.'
    assert insights.snap_labels(text, labels) == 'The "Sum([SUM]) *special*" measure.'


# -- shares on an ad-hoc query ------------------------------------------

def query_result(rows, columns, total_rows=None):
    return {
        "columns": columns, "rows": [dict(r) for r in rows],
        "returned_rows": len(rows),
        "total_rows": total_rows if total_rows is not None else len(rows),
    }


def test_query_rows_carry_their_share():
    result = insights.add_shares(
        FakeEngine(),
        query_result([{"Region": "Almaty", "m": 80.0}, {"Region": "Astana", "m": 20.0}],
                     ["Region", "m"]),
        dimensions=["Region"], measures=["Sum([SUM])"],
    )

    assert [row["share_pct"] for row in result["rows"]] == [80.0, 20.0]
    assert result["total"] == 100.0


def test_a_top_n_share_is_against_the_real_total_not_the_page():
    """The trap: three rows summing to 140 out of a book of 200 are 70% of
    the book, not 100% of itself."""
    class Engine(FakeEngine):
        def query(self, dimensions=None, measures=None, limit=50, sort_by_measure=True):
            assert not dimensions, "the grand total is read without a dimension"
            return {"columns": ["m"], "rows": [{"m": 200.0}], "total_rows": 1}

    result = insights.add_shares(
        Engine(),
        query_result([{"R": "a", "m": 80.0}, {"R": "b", "m": 60.0}], ["R", "m"], total_rows=20),
        dimensions=["R"], measures=["Sum([SUM])"],
    )

    assert result["total"] == 200.0
    assert result["total_is_grand_total"] is True
    assert [row["share_pct"] for row in result["rows"]] == [40.0, 30.0]


def test_no_share_when_the_real_total_cannot_be_read():
    """Better no percentage than one against the wrong denominator."""
    class Engine(FakeEngine):
        def query(self, **kwargs):
            raise RuntimeError("engine busy")

    result = insights.add_shares(
        Engine(),
        query_result([{"R": "a", "m": 80.0}], ["R", "m"], total_rows=20),
        dimensions=["R"], measures=["Sum([SUM])"],
    )

    assert "share_pct" not in result["rows"][0]
    assert "top 1 of 20" in result["note"]


def test_no_share_on_a_measure_that_does_not_add_up():
    result = insights.add_shares(
        FakeEngine(),
        query_result([{"R": "a", "m": 80.0}], ["R", "m"]),
        dimensions=["R"], measures=["Avg([SUM])"],
    )

    assert "share_pct" not in result["rows"][0]
    assert "do not state a total" in result["note"]


def test_a_measure_only_query_is_left_alone():
    """One aggregated number has no share to take."""
    result = insights.add_shares(
        FakeEngine(), query_result([{"m": 80.0}], ["m"]),
        dimensions=[], measures=["Sum([SUM])"],
    )

    assert "total" not in result
    assert "share_pct" not in result["rows"][0]


def test_text_in_the_measure_column_stops_the_shares():
    result = insights.add_shares(
        FakeEngine(),
        query_result([{"R": "a", "m": "-"}, {"R": "b", "m": 1.0}], ["R", "m"]),
        dimensions=["R"], measures=["Sum([SUM])"],
    )

    assert "share_pct" not in result["rows"][0]


def test_an_empty_result_is_returned_unchanged():
    empty = query_result([], ["R", "m"])
    assert insights.add_shares(FakeEngine(), empty, dimensions=["R"], measures=["Sum([X])"]) is empty
