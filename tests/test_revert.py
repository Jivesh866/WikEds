import sqlite3

import pandas as pd

from wikeds import alerts, anomaly, detector, flags
from wikeds.summary_store import Store

NOW = 1_800_000_030
COLS = ["event_time", "wiki", "server_name", "type", "title", "user", "bot", "old_len", "new_len",
        "namespace", "rev_new", "rev_old", "comment"]


def mem_store():
    s = Store(sqlite3.connect(":memory:"), "?", "mem")
    s.ensure_schema()
    return s


def base_rows():
    return [(NOW - 10 * i, "enwiki", "en.wikipedia.org", "edit", f"n{i}", f"u{i}", 0, 1000,
             1000 + (i % 5 - 2) * 10, 0, 100 + i, 99 + i, "copyedit") for i in range(1, 61)]


def frame(extra):
    return pd.DataFrame(base_rows() + extra, columns=COLS)


BLANK = (NOW - 120, "enwiki", "en.wikipedia.org", "edit", "Victim", "203.0.113.9", 0, 9000, 0, 0, 555, 554, "")


def test_is_revert_matches_common_summaries():
    s = pd.Series(["Reverted edits by X", "Undid revision 123", "rv vandalism", "Reverting possible test",
                   "[[WP:UNDO|Undid]] edit", "copyedit", "Corrected a revert-worthy typo?", None, "rollback"])
    assert list(anomaly.is_revert(s)) == [True, True, True, True, True, False, True, False, True]


def test_edit_that_is_itself_a_revert_is_not_flagged():
    cleanup = (NOW - 60, "enwiki", "en.wikipedia.org", "edit", "Cleaned", "Patroller", 0, 9000, 0, 0, 7, 6,
               "Reverted edits by 203.0.113.9 (talk) to last version by Bob")
    df = frame([BLANK, cleanup])
    assert set(anomaly.suspicious_edits(df, NOW)["title"]) == {"Victim", "Cleaned"}
    assert set(anomaly.suspicious_edits(df, NOW, skip_reverts=True)["title"]) == {"Victim"}


def test_detector_keeps_diff_ids_comment_and_marks_a_later_revert():
    later = (NOW - 30, "enwiki", "en.wikipedia.org", "edit", "Victim", "Patroller", 0, 0, 9000, 0, 556, 555,
             "Reverted edits by 203.0.113.9")
    selfrev = (NOW - 25, "enwiki", "en.wikipedia.org", "edit", "Other", "Same", 0, 0, 9000, 0, 9, 8, "undo")
    blank2 = (NOW - 90, "enwiki", "en.wikipedia.org", "edit", "Other", "Same", 0, 9000, 0, 0, 8, 7, "")
    found = {f["title"]: f for f in detector.collect(frame([BLANK, later, selfrev, blank2]), NOW)
             if f["kind"] == "vandalism"}
    v = found["Victim"]
    assert (v["rev_new"], v["rev_old"], v["reverted"]) == (555, 554, 1)
    assert found["Other"]["reverted"] == 0          # undone by the same person: not evidence


def test_flags_roundtrip_reverted_and_diff_link():
    s = mem_store()
    f = {"kind": "vandalism", "wiki": "enwiki", "subject": "S", "title": "Victim", "editor": "203.0.113.9",
         "server": "en.wikipedia.org", "reason": "page blanked", "z": None, "edits": 1, "editors": 1,
         "bytes_changed": -9000, "rev_new": 555, "rev_old": 554, "comment": "blanked", "reverted": 0}
    flags.save(s, [f], 1000, gap=1800)
    flags.save(s, [dict(f, reverted=1)], 1060, gap=1800)     # revert noticed a minute later
    flags.save(s, [dict(f, reverted=0)], 1120, gap=1800)     # never goes back
    row = flags.read(s, 0).iloc[0]
    assert row["reverted"] == 1 and row["comment"] == "blanked"
    assert flags.diff_url("en.wikipedia.org", row["rev_new"], row["rev_old"]) == \
        "https://en.wikipedia.org/w/index.php?diff=555&oldid=554"
    assert flags.diff_url("en.wikipedia.org", 555, None) == "https://en.wikipedia.org/w/index.php?oldid=555"
    assert flags.diff_url("en.wikipedia.org", float("nan"), None) is None
    assert flags.precision(flags.read(s, 0)).iloc[0]["reverted"] == 1


def test_old_flags_table_gets_the_new_columns_and_keeps_its_rows():
    con = sqlite3.connect(":memory:")
    con.execute("""CREATE TABLE anomaly_flags (flag_id TEXT PRIMARY KEY, kind TEXT NOT NULL, wiki TEXT NOT NULL,
        subject TEXT NOT NULL, title TEXT NOT NULL, editor TEXT NOT NULL, server TEXT NOT NULL,
        reason TEXT NOT NULL, peak_z DOUBLE PRECISION, edits BIGINT NOT NULL, editors BIGINT NOT NULL,
        bytes_changed BIGINT NOT NULL, first_seen BIGINT NOT NULL, last_seen BIGINT NOT NULL,
        label TEXT, labeled_at BIGINT)""")
    con.execute("INSERT INTO anomaly_flags VALUES ('old|1', 'burst', 'enwiki', 'P', 'P', '', 's', 'r', 5.0, "
                "8, 4, 0, 10, 20, 'real news', 30)")
    con.commit()
    s = Store(con, "?", "mem")
    df = flags.read(s, 0)                         # runs the migration
    assert df.iloc[0]["label"] == "real news" and df.iloc[0]["reverted"] == 0 and pd.isna(df.iloc[0]["comment"])
    flags.save(s, [{"kind": "burst", "wiki": "enwiki", "subject": "Q", "title": "Q", "editor": "",
                    "server": "s", "reason": "r", "z": 6.0, "edits": 7, "editors": 3, "bytes_changed": 0}], 100)
    assert len(flags.read(s, 0)) == 2


class Fake:
    def __init__(self):
        self.calls = []

    def __call__(self, url, json, timeout):
        self.calls.append(json)

        class R:
            status_code = 204
        return R()


def test_alert_message_has_diff_summary_and_revert_note():
    s, post = mem_store(), Fake()
    flags.save(s, [{"kind": "vandalism", "wiki": "enwiki", "subject": "S", "title": "Victim",
                    "editor": "203.0.113.9", "server": "en.wikipedia.org", "reason": "page blanked", "z": None,
                    "edits": 1, "editors": 1, "bytes_changed": -9000, "rev_new": 555, "rev_old": 554,
                    "comment": "lol *bold* @everyone", "reverted": 1}], NOW)
    assert alerts.send_new(s, NOW, "https://x/y", 10, post) == (1, False)
    e = post.calls[0]["embeds"][0]["description"]
    assert "[Diff](https://en.wikipedia.org/w/index.php?diff=555&oldid=554)" in e
    assert "Already reverted." in e and "\\*bold\\*" in e and "\\@everyone" in e