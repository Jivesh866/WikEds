import sqlite3
import time

from wikeds import config, listener

NOW = int(time.time())


def _seeded(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "w.db"))
    monkeypatch.setattr(config, "RUN_ENV", "local", raising=False)
    l = listener.Listener()
    rows = [("filler", NOW - 7200, NOW - 7200, "enwiki", "en.wikipedia.org", "edit", "Filler",
             "x", 0, 0, 0, 100, 110, None, None, None)]
    rows += [(f"b{i}", NOW - 600 + i * 10, NOW - 600 + i * 10, "enwiki", "en.wikipedia.org",
              "edit", "Breaking", f"u{i % 6}", 0, 0, 0, 100, 110, None, None, None) for i in range(10)]
    l.con.executemany(listener.INSERT_SQL, rows)
    l.con.commit()
    return l


def _break_store(monkeypatch):
    def boom():
        raise RuntimeError("connection refused")
    monkeypatch.setattr(listener, "open_store", boom)


def test_detector_failure_shows_up_in_status(tmp_path, monkeypatch):
    l = _seeded(tmp_path, monkeypatch)
    _break_store(monkeypatch)
    l._detect()
    s = listener.STATUS["detect"]
    assert s["ok"] is False and "connection refused" in s["err"]


def test_rollup_failure_and_success_show_up_in_status(tmp_path, monkeypatch):
    l = _seeded(tmp_path, monkeypatch)
    _break_store(monkeypatch)
    l._maintenance()
    assert listener.STATUS["rollup"]["ok"] is False
    monkeypatch.undo()
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "w.db"))
    monkeypatch.setattr(config, "SUMMARY_DB_URL", "")
    l._maintenance()
    assert listener.STATUS["rollup"]["ok"] is True


def test_first_maintenance_is_due_after_about_a_minute(tmp_path, monkeypatch):
    l = _seeded(tmp_path, monkeypatch)
    wait = config.CLEANUP_EVERY_SECONDS - (time.time() - l.last_cleanup)
    assert 55 <= wait <= 65