import pytest

import config
import doctor
import users


@pytest.fixture
def report(tmp_path):
    return str(tmp_path / "report.txt")


def run(report, *extra):
    return doctor.main(["--out", report, *extra])


class TestItAlwaysProducesAReport:
    def test_it_survives_everything_being_broken(self, isolated_accounts,
                                                 report, monkeypatch, capsys):
        monkeypatch.setattr(config, "QLIK_HOST", "nowhere.invalid")
        monkeypatch.setattr(config, "QLIK_CERT_DIR", "")
        monkeypatch.setattr(config, "OPENAI_BASE_URL", "http://127.0.0.1:9/v1")

        code = run(report)

        assert code in (0, 1), "the report must never crash the way it exits"
        body = capsys.readouterr().out
        for heading in ("WHICH CODE IS THIS", "CONFIGURATION", "ACCOUNTS",
                        "QLIK", "MODEL ENDPOINT", "CHART TYPES", "SUMMARY"):
            assert heading in body, heading

    def test_a_broken_section_does_not_lose_the_rest(self, isolated_accounts,
                                                     report, monkeypatch,
                                                     capsys):
        def explode():
            raise RuntimeError("the accounts table is on fire")

        monkeypatch.setattr(users, "listing", explode)

        code = run(report)
        body = capsys.readouterr().out

        assert code == 1
        assert "on fire" in body
        assert "SUMMARY" in body, (
            "one failing section must not stop the sections after it - that is "
            "the whole point of collecting everything in one pass"
        )

    def test_it_writes_the_file_to_paste_back(self, isolated_accounts, report):
        run(report)

        with open(report, encoding="utf-8") as handle:
            saved = handle.read()

        assert "QLIK AI - DEPLOYMENT REPORT" in saved
        assert "SUMMARY" in saved

    def test_skipping_the_slow_parts_still_reports(self, isolated_accounts,
                                                   report, capsys):
        run(report, "--skip-qlik", "--skip-model")
        body = capsys.readouterr().out

        assert "skipped (--skip-qlik)" in body
        assert "skipped (--skip-model)" in body
        assert "SUMMARY" in body


class TestItNeverLeaksASecret:
    def test_the_api_key_is_not_in_the_report(self, isolated_accounts, report,
                                              monkeypatch, capsys):
        secret = "sk-proj-DO-NOT-PRINT-THIS-0123456789"
        monkeypatch.setattr(config, "OPENAI_API_KEY", secret)

        run(report, "--skip-qlik")
        body = capsys.readouterr().out
        with open(report, encoding="utf-8") as handle:
            saved = handle.read()

        assert secret not in body, "the report is meant to be pasted back"
        assert secret not in saved

    def test_a_password_is_not_in_the_report(self, isolated_accounts, report,
                                             monkeypatch, capsys):
        monkeypatch.setattr(config, "ADMIN_PASSWORD", "hunter2-do-not-print")

        run(report, "--skip-qlik", "--skip-model")
        body = capsys.readouterr().out

        assert "hunter2" not in body


class TestItSaysWhichCodeIsRunning:
    def test_it_names_the_features_present(self, isolated_accounts, report,
                                           capsys):
        run(report, "--skip-qlik", "--skip-model")
        body = capsys.readouterr().out

        for feature in ("identity guard", "landing-app fallback",
                        "startup checks", "repository client"):
            assert feature in body, feature

    def test_a_missing_feature_is_called_out(self, isolated_accounts, report,
                                             monkeypatch, capsys):
        import session

        monkeypatch.delattr(session, "QlikIdentityMissing", raising=False)

        run(report, "--skip-qlik", "--skip-model")
        body = capsys.readouterr().out

        assert "NO - this copy predates it" in body, (
            "running an old copy on the server has been mistaken for a bug "
            "more than once; the report has to say so"
        )
