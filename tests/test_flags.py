import sqlite3

import pandas as pd

from wikeds import detector, flags, listener
from wikeds.summary_store import Store

NOW = 1_800_000_030
GAP = 1800


def mem_store():
    s = Store(sqlite3.connect(":memory:"), "?", "mem")
    s.ensure_schema()
    return s


def flag(kind="burst", wiki="enwiki", subject="Page", z=6.0, edits=8, editors=4):
    return {"kind": kind, "wiki": wiki, "subject": subject, "title": subject, "editor": "",
            "server": "en.wikipedia.org", "reason": "why", "z": z, "edits": edits,
            "editors": editors, "bytes_changed": 0}


def test_same_episode_is_updated_not_duplicated():
    s = mem_store()
    assert flags.save(s, [flag()], 1000, gap=GAP) == (1, 0)
    assert flags.save(s, [flag(z=9.0, edits=12, editors=5)], 1060, gap=GAP) == (0, 1)
    assert flags.save(s, [flag(z=5.0, edits=7)], 1120, gap=GAP) == (0, 1)   # lower, keeps the peak

    df = flags.read(s, 0)
    assert len(df) == 1
    row = df.iloc[0]
    assert row["peak_z"] == 9.0 and row["edits"] == 12 and row["editors"] == 5
    assert row["first_seen"] == 1000 and row["last_seen"] == 1120


def test_new_episode_after_a_quiet_gap_and_separate_subjects():
    s = mem_store()
    flags.save(s, [flag(subject="A"), flag(subject="B")], 1000, gap=GAP)
    assert len(flags.read(s, 0)) == 2
    flags.save(s, [flag(subject="A")], 1000 + GAP + 10, gap=GAP)    # quiet too long -> new row
    df = flags.read(s, 0)
    assert len(df) == 3 and (df["title"] == "A").sum() == 2


def test_labels_and_precision():
    s = mem_store()
    flags.save(s, [flag(subject="A"), flag(subject="B"), flag(subject="C"),
                   flag(kind="spike", subject="")], 1000, gap=GAP)
    ids = dict(zip(flags.read(s, 0)["title"], flags.read(s, 0)["flag_id"]))
    flags.set_label(s, ids["A"], "real news")
    flags.set_label(s, ids["B"], "false alarm")
    flags.set_label(s, ids["C"], "unsure")
    flags.set_label(s, ids["C"], None)                                # clearing works
    df = flags.read(s, 0)
    assert pd.isna(df.set_index("title").loc["C", "label"])

    p = flags.precision(df, {"burst": "News burst"}).set_index("detector")
    assert p.loc["News burst", "flags"] == 3 and p.loc["News burst", "reviewed"] == 2
    assert p.loc["News burst", "real"] == 1 and p.loc["News burst", "precision"] == "50%"
    assert p.loc["spike", "precision"] == "n/a"

    try:
        flags.set_label(s, ids["A"], "bogus")
        assert False, "should have raised"
    except ValueError:
        pass


def test_prune_and_read_window():
    s = mem_store()
    flags.save(s, [flag(subject="old")], 1000, gap=GAP)
    flags.save(s, [flag(subject="new")], 5000, gap=GAP)
    assert list(flags.read(s, 2000)["title"]) == ["new"]
    assert flags.prune(s, 2000) == 1
    assert list(flags.read(s, 0)["title"]) == ["new"]


def test_detector_collect_returns_plain_types():
    """Raw sqlite -> detector -> flag dicts that psycopg can store (no numpy types)."""
    con = sqlite3.connect(":memory:")
    con.executescript(listener.SCHEMA)
    rows = [("filler", NOW - 7200, NOW - 7200, "enwiki", "en.wikipedia.org", "edit", "Filler",
             "x", 0, 0, 0, 100, 110, None, None, None)]
    rows += [(f"b{i}", NOW - 600 + i * 10, NOW - 600 + i * 10, "enwiki", "en.wikipedia.org",
              "edit", "Breaking", f"u{i % 6}", 0, 0, 0, 100, 110, None, None, None) for i in range(10)]
    con.executemany(listener.INSERT_SQL, rows)
    con.commit()

    found = detector.detect(con, NOW)
    assert len(found) == 1
    f = found[0]
    assert f["kind"] == "burst" and f["subject"] == "Breaking" and f["server"] == "en.wikipedia.org"
    assert type(f["edits"]) is int and type(f["editors"]) is int and type(f["z"]) is float
    assert f["edits"] == 10 and f["editors"] == 6 and f["bytes_changed"] == 100

    s = mem_store()                                  # and it round-trips through the store
    assert flags.save(s, found, NOW, gap=GAP) == (1, 0)
    assert isinstance(flags.read(s, 0), pd.DataFrame)