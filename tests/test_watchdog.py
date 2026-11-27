import pytest

from wikeds import config, listener

T0 = 1_800_000_000.0


class Fake:
    def __init__(self, status=204):
        self.status, self.calls = status, []

    def __call__(self, url, json, timeout):
        self.calls.append(json["embeds"][0]["title"])

        class R:
            status_code = self.status
        return R()


@pytest.fixture
def dog(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "w.db"))
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "https://discord.example/x")
    monkeypatch.setattr(config, "DETECT_ENABLED", True)
    listener.STATUS["rollup"], listener.STATUS["detect"] = {}, {}
    l = listener.Listener()
    l.started_at = l.last_event_at = T0
    return l


def healthy(l, now):
    """Pretend the jobs worked just now."""
    l.last_event_at = now
    listener.STATUS["rollup"] = {"at": now, "ok": True, "err": None, "ok_at": now}
    listener.STATUS["detect"] = {"at": now, "ok": True, "err": None, "ok_at": now}


def test_healthy_app_posts_nothing(dog):
    post = Fake()
    healthy(dog, T0 + 30)
    dog.watchdog_tick(T0 + 30, post)
    assert post.calls == [] and dog.check_problems(T0 + 30) == {}


def test_stalled_ingest_posts_once_then_recovers(dog):
    post = Fake()
    t = T0 + config.STALL_SECONDS + 10
    healthy(dog, T0)
    dog.last_event_at = T0                       # nothing since the start
    listener.STATUS["rollup"]["ok_at"] = listener.STATUS["detect"]["ok_at"] = t
    dog.watchdog_tick(t, post)
    dog.watchdog_tick(t + 30, post)              # still down: no second message
    assert post.calls == ["Problem: ingest stalled"]
    dog.last_event_at = t + 40
    dog.watchdog_tick(t + 60, post)
    assert post.calls == ["Problem: ingest stalled", "Recovered: ingest stalled"]
    dog.watchdog_tick(t + 90, post)
    assert len(post.calls) == 2                  # and nothing more


def test_failed_post_is_retried_and_long_outage_reminds(dog, monkeypatch):
    t = T0 + config.STALL_SECONDS + 10
    healthy(dog, T0)
    dog.last_event_at = T0
    listener.STATUS["rollup"]["ok_at"] = listener.STATUS["detect"]["ok_at"] = t
    dog.watchdog_tick(t, Fake(429))              # discord says no
    assert dog.problems == {}
    good = Fake()
    dog.watchdog_tick(t + 30, good)
    assert good.calls == ["Problem: ingest stalled"]
    monkeypatch.setattr(config, "OUTAGE_REMIND_SECONDS", 600)
    listener.STATUS["rollup"]["ok_at"] = listener.STATUS["detect"]["ok_at"] = t + 700
    dog.watchdog_tick(t + 700, good)
    assert good.calls == ["Problem: ingest stalled"] * 2


def test_rollup_and_detector_failures_are_reported(dog):
    post = Fake()
    now = T0 + config.ROLLUP_STALE_SECONDS + 60
    dog.last_event_at = now                      # stream is fine
    listener.STATUS["rollup"] = {"at": now, "ok": False, "err": "boom", "ok_at": T0}
    listener.STATUS["detect"] = {"at": now, "ok": False, "err": "boom", "ok_at": T0}
    problems = dog.check_problems(now)
    assert set(problems) == {"rollup", "anomaly detector"}
    assert "Health tab" in problems["rollup"] and "boom" not in problems["rollup"]   # error text stays off Discord
    dog.watchdog_tick(now, post)
    assert sorted(post.calls) == ["Problem: anomaly detector", "Problem: rollup"]


def test_detector_switched_off_is_not_a_problem(dog, monkeypatch):
    monkeypatch.setattr(config, "DETECT_ENABLED", False)
    healthy(dog, T0 + 4000)
    listener.STATUS["detect"] = {}
    assert dog.check_problems(T0 + 4000) == {}


def test_no_webhook_means_no_watchdog(dog, monkeypatch):
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "")
    dog.start_watchdog()
    assert dog.watchdog_thread is None