"""Proving a save reached disk.

Qlik Sense Desktop keeps its own in-memory copy of an open document. A sheet
created through the Engine API is genuinely written to the .qvf and still
invisible in a Qlik window that already had the app open - which reads as
"the software didn't do anything". The file's timestamp is what separates
"it didn't save" from "Qlik is showing a cached copy".
"""

from datetime import datetime

import pytest

from qlik_engine import QLIK_EPOCH

DOC_LIST = {"qDocList": [
    {
        "qDocId": "C:\\qlik\\doc\\Sense\\Apps\\data.qvf",
        "qDocName": "data.qvf",
        "qTitle": "data",
        "qFileSize": 9932800,
        # 2026-08-03 14:33:31 as a Qlik serial date.
        "qFileTime": 46237.6066087963,
    },
    {"qDocId": "other.qvf", "qTitle": "other", "qFileSize": 1, "qFileTime": 1.0},
]}


class TestQlikEpoch:

    def test_matches_the_excel_epoch(self):
        assert QLIK_EPOCH == datetime(1899, 12, 30)

    def test_a_serial_converts_to_a_real_date(self):
        from datetime import timedelta
        converted = QLIK_EPOCH + timedelta(days=46237.6066087963)
        assert converted.year == 2026 and converted.month == 8


class TestAppFileInfo:

    @pytest.fixture
    def ready(self, engine):
        engine.ws.handlers = {"GetDocList": DOC_LIST}
        engine.app_id = "C:\\qlik\\doc\\Sense\\Apps\\data.qvf"
        engine.app_name = "data"
        return engine

    def test_reports_the_path_and_size(self, ready):
        info = ready.app_file_info()
        assert info["path"].endswith("data.qvf")
        assert info["size_bytes"] == 9932800

    def test_reports_a_human_readable_timestamp(self, ready):
        modified = ready.app_file_info()["modified"]
        assert modified.startswith("2026-08-03")
        assert ":" in modified

    def test_matches_by_title_when_the_id_differs(self, engine):
        engine.ws.handlers = {"GetDocList": DOC_LIST}
        engine.app_id = "not-a-match"
        engine.app_name = "data"
        assert engine.app_file_info()["size_bytes"] == 9932800

    def test_returns_none_for_an_unknown_app(self, engine):
        engine.ws.handlers = {"GetDocList": DOC_LIST}
        engine.app_id = "ghost"
        engine.app_name = "ghost"
        assert engine.app_file_info() is None

    def test_a_failing_doc_list_is_not_fatal(self, engine):
        """Reporting the timestamp is a nicety; losing it must not break save."""
        engine.ws.handlers = {"GetDocList": {"error": {"message": "nope"}}}
        engine.app_id = engine.app_name = "data"
        assert engine.app_file_info() is None

    def test_a_missing_file_time_is_tolerated(self, engine):
        engine.ws.handlers = {"GetDocList": {"qDocList": [
            {"qDocId": "d", "qTitle": "data", "qFileSize": 5},
        ]}}
        engine.app_id = engine.app_name = "data"
        assert engine.app_file_info()["modified"] is None


class TestThePageExplainsIt:

    def test_the_note_tells_the_user_to_reopen_the_app(self):
        import web_app

        html = web_app.INDEX.read_text(encoding="utf-8")
        assert "noteQlikCache" in html
        assert "close the app in Qlik" in html

    def test_the_note_warns_against_saving_from_qlik(self):
        """Qlik's older in-memory copy would overwrite what was just written."""
        import web_app

        html = web_app.INDEX.read_text(encoding="utf-8")
        assert "overwrite" in html
