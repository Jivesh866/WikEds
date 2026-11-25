import sqlite3

import pytest

from wikeds.summary_store import Store


@pytest.fixture
def store():
    s = Store(sqlite3.connect(":memory:"), "?", "mem")
    s.ensure_schema()
    return s


def hour(h, edits, editors=1, net=0, wiki="enwiki", typ="edit", bot=0):
    return (h, wiki, typ, bot, edits, editors, net)


def edits_at(store, h):
    df = store.read_hours(0)
    return int(df.loc[df["hour_start"] == h, "edits"].iloc[0])


def test_meta_roundtrip(store):
    assert store.get_meta("x") is None
    store.set_meta("x", 5)
    store.set_meta("x", 7)
    assert store.get_meta("x") == 7


def test_upsert_only_replaces_with_equal_or_bigger(store):
    store.upsert_hours([hour(3600, 10)])
    store.upsert_hours([hour(3600, 5)])
    assert edits_at(store, 3600) == 10
    store.upsert_hours([hour(3600, 12)])
    assert edits_at(store, 3600) == 12


def test_read_since_and_prune(store):
    store.upsert_hours([hour(3600, 1), hour(7200, 2), hour(10800, 3)])
    assert list(store.read_hours(7200)["hour_start"]) == [7200, 10800]
    assert store.prune(7200) == 1
    assert list(store.read_hours(0)["hour_start"]) == [7200, 10800]


def test_empty_read_has_the_right_columns(store):
    df = store.read_hours(0)
    assert df.empty and "net_bytes" in df.columns