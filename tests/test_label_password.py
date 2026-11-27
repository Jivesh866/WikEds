import sqlite3
import time

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest

from wikeds import config, flag_review, flags
from wikeds.summary_store import open_store


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "w.db"))
    store = open_store()
    flags.save(store, [{"kind": "burst", "wiki": "enwiki", "subject": "P", "title": "P", "editor": "",
                        "server": "en.wikipedia.org", "reason": "r", "z": 6.0, "edits": 8, "editors": 4,
                        "bytes_changed": 0}], int(time.time()))
    store.close()


def panel():
    from wikeds import flag_review
    flag_review.flags_panel()


def test_password_check(monkeypatch):
    monkeypatch.setattr(config, "LABEL_PASSWORD", "s3cret")
    assert flag_review.password_ok("s3cret") and not flag_review.password_ok("S3cret")
    assert not flag_review.password_ok("")
    monkeypatch.setattr(config, "LABEL_PASSWORD", "")
    assert not flag_review.password_ok("")          # no password set: nothing unlocks it


def run(monkeypatch, password, cloud):
    monkeypatch.setattr(config, "LABEL_PASSWORD", password)
    monkeypatch.setattr(config, "IS_CLOUD", cloud)
    return AppTest.from_function(panel, default_timeout=30).run()


def test_locked_until_the_password_is_entered(seeded, monkeypatch):
    at = run(monkeypatch, "s3cret", True)
    assert not at.exception
    assert [t.label for t in at.text_input] == ["Password to edit labels"]
    assert any("read-only" in c.value for c in at.caption)
    assert len(at.dataframe) >= 1                      # the page is still readable

    at.text_input[0].set_value("wrong"); at.button[0].click(); at.run()
    assert any("Wrong password" in e.value for e in at.error)

    at.text_input[0].set_value("s3cret"); at.button[0].click(); at.run()
    assert at.session_state["labels_unlocked"] is True
    assert any("unlocked" in c.value for c in at.caption)
    assert not any("read-only" in c.value for c in at.caption)


def test_repeated_wrong_passwords_pause_guessing(seeded, monkeypatch):
    monkeypatch.setattr(flag_review.time, "sleep", lambda s: None)
    at = run(monkeypatch, "s3cret", True)
    for _ in range(flag_review.MAX_TRIES):
        at.text_input[0].set_value("nope"); at.button[0].click(); at.run()
    at.text_input[0].set_value("s3cret"); at.button[0].click(); at.run()   # right one, but paused
    assert any("Too many" in e.value for e in at.error)
    assert not at.session_state["labels_unlocked"] if "labels_unlocked" in at.session_state else True


def test_no_password_cloud_is_locked_local_is_open(seeded, monkeypatch):
    at = run(monkeypatch, "", True)
    assert any("LABEL_PASSWORD" in i.value for i in at.info) and not at.text_input
    assert any("read-only" in c.value for c in at.caption)
    at = run(monkeypatch, "", False)
    assert not at.text_input and any("Pick a label" in c.value for c in at.caption)