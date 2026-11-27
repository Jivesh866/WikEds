import sqlite3

from wikeds import listener, rollup
from wikeds.summary_store import Store

OFF = 19_800                 # IST, UTC+5:30
BASE = 1_795_577_400         # start of an IST hour (BASE + OFF is a multiple of 3600)


def raw_db(rows=()):
    con = sqlite3.connect(":memory:")
    con.executescript(listener.SCHEMA)
    add(con, rows)
    return con


def add(con, rows):
    con.executemany(listener.INSERT_SQL, rows)
    con.commit()


_n = 0


def row(t, bot=0, user="u", old=100, new=150, wiki="enwiki"):
    global _n
    _n += 1
    return (f"id{_n}", t, t, wiki, "en.wikipedia.org", "edit", "P", user, bot, 0, 0, old, new, None, None, None)


def mem_store():
    s = Store(sqlite3.connect(":memory:"), "?", "mem")
    s.ensure_schema()
    return s


def totals(store):
    df = store.read_hours(0)
    return df.groupby(["hour_start", "bot"])["edits"].sum().to_dict()


def test_bucket_start_follows_local_hours():
    assert rollup.bucket_start(BASE + 10, OFF) == BASE
    assert rollup.bucket_start(BASE + 3599, OFF) == BASE
    assert rollup.bucket_start(BASE - 1, OFF) == BASE - 3600


def test_groups_by_hour_and_bot():
    raw = raw_db([row(BASE + 10, user="a"), row(BASE + 20, user="b"),
                  row(BASE + 30, bot=1, user="bot"), row(BASE + 3605, user="a")])
    store = mem_store()
    now = BASE + 3 * 3600
    stats = rollup.run_rollup(raw, store, now=now, offset=OFF)

    assert totals(store) == {(BASE, 0): 2, (BASE, 1): 1, (BASE + 3600, 0): 1}
    assert store.get_meta("bucket_offset") == OFF
    assert store.get_meta("last_rollup_at") is not None
    assert stats["rolled_through"] == BASE + 2 * 3600       # last complete hour
    hour0 = store.read_hours(0).query("hour_start == @BASE and bot == 0").iloc[0]
    assert hour0["editors"] == 2 and hour0["net_bytes"] == 100


def test_recompute_never_shrinks_an_hour():
    """Cloud restart wipes raw rows; the recomputed partial hour must not overwrite."""
    store = mem_store()
    now = BASE + 1800                                        # still inside the hour
    raw = raw_db([row(BASE + 10), row(BASE + 20), row(BASE + 30, bot=1)])
    rollup.run_rollup(raw, store, now=now, offset=OFF)
    assert totals(store)[(BASE, 0)] == 2

    raw.execute("DELETE FROM edits")                         # simulate the wipe
    add(raw, [row(BASE + 100)])
    rollup.run_rollup(raw, store, now=now, offset=OFF)
    assert totals(store) == {(BASE, 0): 2, (BASE, 1): 1}     # unchanged

    add(raw, [row(BASE + 200), row(BASE + 300), row(BASE + 400)])
    rollup.run_rollup(raw, store, now=now, offset=OFF)
    assert totals(store)[(BASE, 0)] == 4                     # grows normally


def test_empty_raw_table_is_a_noop():
    store = mem_store()
    stats = rollup.run_rollup(raw_db(), store, now=BASE, offset=OFF)
    assert stats["rows_written"] == 0 and store.read_hours(0).empty