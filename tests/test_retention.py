from datetime import datetime, timedelta, timezone

import pytest

import history
import manage_users
import users

pytestmark = pytest.mark.live_auth


def days_ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(
        timespec="seconds")


def age_audit(action, days):
    users.audit(action)
    users.connect().execute(
        "UPDATE audit SET at = ? WHERE id = (SELECT MAX(id) FROM audit)",
        (days_ago(days),))


def age_chat(owner, days, title="an old question"):
    chat_id = history.new_id()
    history.save(chat_id, [{"role": "user", "content": title}], "data", owner=owner)
    record = history.load(chat_id, owner)
    record["updated"] = days_ago(days)
    history._write(chat_id, record, owner)
    return chat_id


class TestNothingIsDeletedByDefault:
    def test_the_audit_trail_is_kept_forever(self):
        from config import AUDIT_RETENTION_DAYS

        assert AUDIT_RETENTION_DAYS == 0

        age_audit("login", days=4000)
        assert users.prune_audit() == 0
        assert len(users.audit_trail()) == 1

    def test_conversations_are_kept_forever(self):
        from config import HISTORY_RETENTION_DAYS

        assert HISTORY_RETENTION_DAYS == 0

        age_chat(None, days=4000)
        assert history.prune_old() == 0
        assert len(history.listing()) == 1

    def test_a_negative_policy_is_treated_as_no_policy(self):
        age_audit("login", days=4000)
        assert users.prune_audit(-30) == 0
        assert len(users.audit_trail()) == 1


class TestPruningTheAuditTrail:

    def test_it_removes_only_what_is_older_than_the_policy(self):
        age_audit("old", days=100)
        age_audit("recent", days=5)

        assert users.prune_audit(30) == 1
        remaining = [e["action"] for e in users.audit_trail()]
        assert "old" not in remaining
        assert "recent" in remaining

    def test_a_dry_run_deletes_nothing(self):
        age_audit("old", days=100)

        assert users.prune_audit(30, dry_run=True) == 1
        assert len(users.audit_trail()) == 1
        assert users.prune_audit(30) == 1

    def test_the_pruning_is_itself_recorded(self):
        age_audit("old", days=100)
        users.prune_audit(30)

        entries = [e for e in users.audit_trail() if e["action"] == "audit.pruned"]
        assert entries
        assert "removed" in entries[0]["detail"]

    def test_nothing_to_remove_writes_no_record(self):
        age_audit("recent", days=1)
        assert users.prune_audit(30) == 0
        assert not [e for e in users.audit_trail() if e["action"] == "audit.pruned"]


class TestPruningConversations:

    def test_it_goes_by_age_not_by_count(self):
        old = age_chat(None, days=400)
        recent = age_chat(None, days=3)

        assert history.prune_old(365) == 1
        assert history.load(old) is None
        assert history.load(recent) is not None

    def test_it_covers_everybody(self):
        alice = history.owner_key(1)
        bob = history.owner_key(2)
        age_chat(alice, days=400)
        age_chat(bob, days=400)
        age_chat(bob, days=1)

        assert history.prune_old(365) == 2
        assert history.listing(alice) == []
        assert len(history.listing(bob)) == 1

    def test_a_dry_run_deletes_nothing(self):
        age_chat(None, days=400)
        assert history.prune_old(365, dry_run=True) == 1
        assert len(history.listing()) == 1

    def test_a_conversation_with_no_timestamp_counts_as_old(self):
        chat_id = history.new_id()
        history.save(chat_id, [{"role": "user", "content": "hello"}])
        record = history.load(chat_id)
        del record["updated"]
        history._write(chat_id, record)

        assert history.prune_old(365) == 1
        assert history.load(chat_id) is None


class TestTheCommand:

    def test_it_says_when_no_policy_is_set(self, capsys):
        assert manage_users.main(["prune"]) == 0
        assert "No retention policy is set" in capsys.readouterr().out

    def test_a_dry_run_reports_without_deleting(self, capsys):
        age_audit("old", days=100)
        age_chat(None, days=400)

        assert manage_users.main(
            ["prune", "--audit-days", "30", "--chat-days", "365", "--dry-run"]) == 0

        out = capsys.readouterr().out
        assert "would remove 1 record" in out
        assert "would remove 1 chat" in out
        assert "Nothing was deleted" in out
        assert len(users.audit_trail()) == 1
        assert len(history.listing()) == 1

    def test_applying_it_removes_both(self, capsys):
        age_audit("old", days=100)
        age_chat(None, days=400)

        assert manage_users.main(
            ["prune", "--audit-days", "30", "--chat-days", "365"]) == 0

        out = capsys.readouterr().out
        assert "removed 1 record" in out
        assert "removed 1 chat" in out
        assert history.listing() == []

    def test_one_policy_without_the_other(self, capsys):
        age_audit("old", days=100)
        age_chat(None, days=400)

        manage_users.main(["prune", "--audit-days", "30"])

        out = capsys.readouterr().out
        assert "Conversations: kept (no policy set)" in out
        assert len(history.listing()) == 1


class TestStartupAppliesIt:

    def test_a_failure_does_not_stop_the_server_starting(self, monkeypatch, capsys):
        import web_app

        def boom(*args, **kwargs):
            raise OSError("the disk is read-only")

        monkeypatch.setattr(users, "prune_audit", boom)
        web_app._apply_retention()

    def test_it_reports_what_it_removed(self, monkeypatch, capsys):
        import web_app

        monkeypatch.setattr(users, "prune_audit", lambda: 7)
        monkeypatch.setattr(history, "prune_old", lambda: 2)
        web_app._apply_retention()

        assert "removed 7 audit record(s), 2 conversation(s)" in capsys.readouterr().out

    def test_it_says_nothing_when_there_is_nothing_to_say(self, monkeypatch, capsys):
        import web_app

        monkeypatch.setattr(users, "prune_audit", lambda: 0)
        monkeypatch.setattr(history, "prune_old", lambda: 0)
        web_app._apply_retention()

        assert capsys.readouterr().out == ""
