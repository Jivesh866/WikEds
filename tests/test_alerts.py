import sqlite3

import requests

from wikeds import alerts, flags
from wikeds.summary_store import Store

NOW = 1_800_000_000
URL = "https://discord.example/api/webhooks/secret-token"


def mem_store():
    s = Store(sqlite3.connect(":memory:"), "?", "mem")
    s.ensure_schema()
    return s


def flag(kind="burst", subject="Page", reason="why", editor="", nbytes=0, z=8.0):
    return {"kind": kind, "wiki": "enwiki", "subject": subject, "title": subject, "editor": editor,
            "server": "en.wikipedia.org", "reason": reason, "z": z, "edits": 9, "editors": 4,
            "bytes_changed": nbytes}


class Fake:
    """Stands in for requests.post and records what would be sent."""
    def __init__(self, status=204):
        self.status, self.calls = status, []

    def __call__(self, url, json, timeout):
        self.calls.append(json)

        class R:
            status_code = self.status
        return R()


def test_only_bursts_and_blanked_pages_alert_and_only_once():
    s, post = mem_store(), Fake()
    flags.save(s, [flag(subject="Breaking"),
                   flag("vandalism", "Gone", "page blanked", "203.0.113.7", -5000),
                   flag("vandalism", "Cut|e|1", "large removal"),
                   flag("spike", "")], NOW)
    assert alerts.send_new(s, NOW, URL, 10, post) == (2, False)
    titles = [c["embeds"][0]["title"] for c in post.calls]
    assert titles == ["News burst: Breaking", "Page blanked: Gone"] or sorted(titles) == sorted(
        ["News burst: Breaking", "Page blanked: Gone"])
    assert alerts.send_new(s, NOW + 60, URL, 10, post) == (0, False)    # nothing repeats
    assert len(post.calls) == 2


def test_still_flagged_episode_does_not_alert_again():
    s, post = mem_store(), Fake()
    flags.save(s, [flag()], NOW)
    alerts.send_new(s, NOW, URL, 10, post)
    flags.save(s, [flag(z=15.0)], NOW + 120)          # same episode, bigger
    assert alerts.send_new(s, NOW + 120, URL, 10, post) == (0, False)
    assert len(post.calls) == 1


def test_rate_limit_folds_the_rest_into_one_message():
    s, post = mem_store(), Fake()
    flags.save(s, [flag(subject=f"P{i}") for i in range(5)], NOW)
    assert alerts.send_new(s, NOW, URL, 2, post) == (2, False)
    assert len(post.calls) == 3 and "3 more" in post.calls[-1]["content"]
    flags.save(s, [flag(subject="Late")], NOW + 600)
    assert alerts.send_new(s, NOW + 600, URL, 2, post) == (0, False)   # budget used up this hour
    assert "1 more" in post.calls[-1]["content"]
    assert alerts.send_new(s, NOW + 4000, URL, 2, post) == (0, False)  # nothing pending, no message


def test_failure_keeps_flags_pending_for_retry():
    s = mem_store()
    flags.save(s, [flag(subject="A"), flag(subject="B")], NOW)
    assert alerts.send_new(s, NOW, URL, 10, Fake(429)) == (0, True)
    good = Fake()
    assert alerts.send_new(s, NOW + 60, URL, 10, good) == (2, False)

    def boom(*a, **k):
        raise requests.ConnectionError(URL)            # message would contain the secret url
    flags.save(s, [flag(subject="C")], NOW + 100)
    assert alerts.send_new(s, NOW + 100, URL, 10, boom) == (0, True)


def test_no_webhook_means_no_alerts_and_no_tables_touched():
    s = mem_store()
    assert alerts.send_new(s, NOW, "", 10, Fake()) == (0, False)


def test_message_is_safe_and_linked():
    body = alerts.build_message({
        "kind": "vandalism", "wiki": "enwiki", "title": "@everyone [x](http://evil)", "editor": "*bold*",
        "server": "en.wikipedia.org", "reason": "page blanked", "peak_z": None, "edits": 1,
        "editors": 1, "bytes_changed": -1234, "first_seen": NOW})
    assert body["allowed_mentions"] == {"parse": []}
    e = body["embeds"][0]
    assert "\\*bold\\*" in e["description"] and "1,234 bytes" in e["description"]
    assert e["url"] == "https://en.wikipedia.org/wiki/%40everyone_%5Bx%5D%28http%3A%2F%2Fevil%29"
    assert "action=history" in e["description"]


def test_old_alert_log_rows_are_pruned():
    s = mem_store()
    flags.save(s, [flag()], NOW)
    alerts.send_new(s, NOW, URL, 10, Fake())
    assert alerts.prune(s, NOW + 10) == 1


def test_blanked_page_by_registered_editor_does_not_alert():
    s, post = mem_store(), Fake()
    flags.save(s, [flag("vandalism", "Moved", "page blanked", "RealEditor", -5000),
                   flag("vandalism", "Spam", "page blanked", "2001:db8::1", -3000)], NOW)
    assert alerts.send_new(s, NOW, URL, 10, post) == (1, False)
    assert post.calls[0]["embeds"][0]["title"] == "Page blanked: Spam"
    assert alerts.send_new(s, NOW + 60, URL, 10, post) == (0, False)    # the skipped one stays quiet


def test_blanked_and_burst_budgets_are_separate():
    s, post = mem_store(), Fake()
    flags.save(s, [flag("vandalism", f"B{i}", "page blanked", f"203.0.113.{i}", -4000) for i in range(5)]
               + [flag(subject="Breaking")], NOW)
    assert alerts.send_new(s, NOW, URL, 10, post, blank_limit=2) == (3, False)   # 2 blanked + the burst
    texts = [c.get("content") or c["embeds"][0]["title"] for c in post.calls]
    assert "News burst: Breaking" in texts
    assert any("3 more blanked page" in t for t in texts)