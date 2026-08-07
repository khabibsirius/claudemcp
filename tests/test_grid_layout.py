"""Sheet layout: charts must not overlap, and must stay on the sheet."""

import itertools

import pytest

from chart_specs import DEFAULT_SIZES


def place_all(engine, sizes):
    return [engine._place_on_grid(w, h) for w, h in sizes]


def overlap(a, b):
    """True if two (col, row, colspan, rowspan) rectangles intersect."""
    a_col, a_row, a_w, a_h = a
    b_col, b_row, b_w, b_h = b
    return not (
        a_col + a_w <= b_col
        or b_col + b_w <= a_col
        or a_row + a_h <= b_row
        or b_row + b_h <= a_row
    )


def assert_no_overlaps(cells):
    for first, second in itertools.combinations(cells, 2):
        assert not overlap(first, second), f"{first} overlaps {second}"


class TestPacking:

    def test_fills_a_row_before_wrapping(self, offline_engine):
        cells = place_all(offline_engine, [(12, 4), (12, 4)])
        assert [(c[0], c[1]) for c in cells] == [(0, 0), (12, 0)]

    def test_wraps_to_a_new_row_when_full(self, offline_engine):
        cells = place_all(offline_engine, [(12, 4), (12, 4), (12, 4)])
        assert cells[2][:2] == (0, 4)

    def test_short_chart_wrapping_under_a_tall_one_does_not_overlap(self, offline_engine):
        """The regression: a row containing a tall chart, wrapped by a short one.

        The packer used to advance by the *incoming* object's rowspan, so a
        3-row KPI wrapping under a 4-row bar chart landed on row 3 - one row
        inside the chart above it.
        """
        cells = place_all(offline_engine, [(12, 4), (6, 3), (6, 3), (6, 3)])

        bar, wrapped = cells[0], cells[3]
        assert bar[:2] == (0, 0)
        assert wrapped[1] >= bar[1] + bar[3], "wrapped cell started inside the row above"
        assert_no_overlaps(cells)

    @pytest.mark.parametrize("sizes", [
        [(6, 3)] * 9,
        [(12, 4), (6, 3), (24, 6), (12, 4), (6, 3), (6, 3)],
        [(24, 6), (24, 6), (12, 4), (12, 4)],
        [(6, 3), (6, 3), (12, 4), (6, 3), (24, 6), (12, 4), (6, 3)],
        list(DEFAULT_SIZES.values()) * 3,
    ])
    def test_never_overlaps_for_mixed_sizes(self, offline_engine, sizes):
        assert_no_overlaps(place_all(offline_engine, sizes))

    def test_oversized_and_zero_spans_are_clamped(self, offline_engine):
        col, row, colspan, rowspan = offline_engine._place_on_grid(99, 0)
        assert colspan == offline_engine.GRID_COLUMNS
        assert rowspan >= 1
        assert (col, row) == (0, 0)


class TestBounds:
    """Qlik positions objects by fractional bounds; col/row are legacy."""

    def cells_for(self, engine, sizes):
        cells = []
        for colspan, rowspan in sizes:
            col, row, colspan, rowspan = engine._place_on_grid(colspan, rowspan)
            cells.append({
                "name": f"obj{len(cells)}", "type": "barchart",
                "col": col, "row": row, "colspan": colspan, "rowspan": rowspan,
            })
        engine._recompute_bounds(cells)
        return cells

    def test_bounds_are_fractions_of_the_sheet(self, offline_engine):
        cells = self.cells_for(offline_engine, [(12, 4), (12, 4)])
        assert cells[0]["bounds"]["x"] == 0.0
        assert cells[0]["bounds"]["width"] == 0.5
        assert cells[1]["bounds"]["x"] == 0.5

    def test_content_fills_the_sheet(self, offline_engine):
        """The regression: seven charts ending at row 18 were divided by a
        fixed 24, leaving the bottom quarter of every sheet empty."""
        sizes = [(12, 4)] * 6 + [(24, 6)]   # what "8 dashboards" produced
        cells = self.cells_for(offline_engine, sizes)

        last = cells[-1]["bounds"]
        assert last["y"] + last["height"] == pytest.approx(1.0), "dead space at the bottom"

    @pytest.mark.parametrize("sizes", [
        [(12, 4), (12, 4)],
        [(12, 4)] * 4,
        [(6, 3)] * 4 + [(12, 4)] * 2 + [(24, 6)],
        [(24, 6)] * 5,
    ])
    def test_the_bottom_edge_is_always_reached(self, offline_engine, sizes):
        cells = self.cells_for(offline_engine, sizes)
        bottom = max(c["bounds"]["y"] + c["bounds"]["height"] for c in cells)
        assert bottom == pytest.approx(1.0)

    def test_a_lone_small_chart_is_not_stretched_to_full_screen(self, offline_engine):
        cells = self.cells_for(offline_engine, [(6, 3)])
        assert cells[0]["bounds"]["height"] < 1.0

    def test_tall_dashboards_stay_on_the_sheet(self, offline_engine):
        """A dashboard taller than the nominal 24-row grid used to produce
        y > 1, putting objects off the bottom of the sheet."""
        cells = self.cells_for(offline_engine, [(24, 6)] * 10)

        for cell in cells:
            bounds = cell["bounds"]
            assert 0.0 <= bounds["y"] <= 1.0
            assert bounds["y"] + bounds["height"] <= 1.0 + 1e-9

    def test_every_cell_is_renormalised_together(self, offline_engine):
        """Adding a tall object rescales the ones already placed, rather than
        leaving them sized against a stale divisor."""
        cells = self.cells_for(offline_engine, [(12, 4)])
        first_height = cells[0]["bounds"]["height"]

        cells = self.cells_for(offline_engine, [(12, 4)] + [(24, 6)] * 8)
        assert cells[0]["bounds"]["height"] < first_height

    def test_handles_preexisting_cells_without_bounds(self, offline_engine):
        cells = [{"name": "old", "type": "kpi", "col": 0, "row": 0, "colspan": 6, "rowspan": 3}]
        offline_engine._recompute_bounds(cells)
        assert cells[0]["bounds"]["width"] == 0.25


class TestGridReset:

    def test_creating_a_sheet_starts_from_the_top_left(self, offline_engine):
        place_all(offline_engine, [(12, 4), (12, 4), (12, 4)])
        offline_engine._reset_grid()
        assert offline_engine._place_on_grid(12, 4)[:2] == (0, 0)
