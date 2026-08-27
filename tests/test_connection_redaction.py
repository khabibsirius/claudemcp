import pytest

from qlik_engine import redact_connection_string

REST_CONNECTION = (
    'CUSTOM CONNECT TO "provider=QvRestConnector.exe;'
    "url=https://data.example.uz/apiPartner/Partner/WebService"
    "?token%26a3a6ac67557b3648803cc8e&name%24-009-0028;"
    'timeout=30;method=GET;authSchema=anonymous;"'
)

SECRETS = ["a3a6ac67557b3648803cc8e", "token%26a3a6ac67557b3648803cc8e"]


class TestRestConnections:

    def test_the_token_is_removed(self):
        result = redact_connection_string(REST_CONNECTION, "QvRestConnector.exe")
        for secret in SECRETS:
            assert secret not in result

    def test_no_query_string_survives(self):
        assert "?" not in redact_connection_string(REST_CONNECTION, "QvRestConnector.exe")

    def test_the_host_is_kept_so_it_is_still_identifiable(self):
        result = redact_connection_string(REST_CONNECTION, "QvRestConnector.exe")
        assert "data.example.uz" in result
        assert "redacted" in result.lower()


class TestOtherConnectionTypes:

    def test_an_unrecognised_string_is_redacted_wholesale(self):
        raw = "OLEDB CONNECT TO [Provider=SQL;Data Source=db;Password=hunter2]"
        result = redact_connection_string(raw, "OLEDB")
        assert "hunter2" not in result
        assert "redacted" in result.lower()

    @pytest.mark.parametrize("path", [
        "C:\\qlik\\doc\\dataset\\",
        "/home/user/data/",
    ])
    def test_folder_paths_pass_through(self, path):
        assert redact_connection_string(path, "folder") == path

    def test_folder_type_is_matched_case_insensitively(self):
        assert redact_connection_string("C:\\data\\", "Folder") == "C:\\data\\"

    @pytest.mark.parametrize("value", ["", None])
    def test_empty_values_are_returned_unchanged(self, value):
        assert redact_connection_string(value, "rest") == value


class TestListConnectionsIntegration:

    def test_list_connections_redacts(self, engine):
        engine.ws.handlers = {"GetConnections": {"qConnections": [
            {
                "qId": "1", "qName": "rest", "qType": "QvRestConnector.exe",
                "qConnectionString": REST_CONNECTION,
            },
            {
                "qId": "2", "qName": "dataset", "qType": "folder",
                "qConnectionString": "C:\\qlik\\doc\\dataset\\",
            },
        ]}}

        connections = {c["name"]: c for c in engine.list_connections()}

        for secret in SECRETS:
            assert secret not in connections["rest"]["path"]
        assert connections["dataset"]["path"] == "C:\\qlik\\doc\\dataset\\"

    def test_names_survive_because_lib_paths_need_them(self, engine):
        engine.ws.handlers = {"GetConnections": {"qConnections": [
            {"qId": "1", "qName": "rest", "qType": "rest",
             "qConnectionString": REST_CONNECTION},
        ]}}
        assert engine.list_connections()[0]["name"] == "rest"
