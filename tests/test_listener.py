from wikeds.listener import parse_event


def _event(**over):
    e = {
        "meta": {"id": "abc"}, "timestamp": 1_700_000_000, "wiki": "enwiki",
        "server_name": "en.wikipedia.org", "type": "edit", "title": "Page",
        "user": "Alice", "bot": False, "minor": True, "namespace": 0,
        "length": {"old": 100, "new": 150},
    }
    e.update(over)
    return e


def test_parses_a_normal_edit():
    row = parse_event(_event())
    assert row[0] == "abc" and row[1] == 1_700_000_000
    assert row[3] == "enwiki" and row[7] == "Alice"
    assert row[8] == 0 and row[9] == 1          # bot, minor as 0/1
    assert row[11:13] == (100, 150)


def test_missing_length_becomes_none():
    row = parse_event(_event(length=None))
    assert row[11] is None and row[12] is None


def test_junk_events_are_skipped():
    assert parse_event({"timestamp": 1}) is None            # no meta id
    assert parse_event(_event(timestamp=None)) is None      # no timestamp
    assert parse_event({}) is None

def test_revision_ids_and_comment_are_kept():
    row = parse_event(_event(revision={"new": 222, "old": 111}, comment="Reverted edits by X"))
    assert row[13:] == (222, 111, "Reverted edits by X")
    plain = parse_event(_event())
    assert plain[13:] == (None, None, None)
    assert len(parse_event(_event(comment="x" * 900))[15]) == 300


def test_open_db_adds_new_columns_to_an_old_database(tmp_path, monkeypatch):
    import sqlite3
    from wikeds import config, listener
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.executescript("CREATE TABLE edits (event_id TEXT PRIMARY KEY, event_time INTEGER NOT NULL,"
                      " ingested_at INTEGER NOT NULL, wiki TEXT, server_name TEXT, type TEXT, title TEXT,"
                      " user TEXT, bot INTEGER, minor INTEGER, namespace INTEGER, old_len INTEGER, new_len INTEGER);"
                      "INSERT INTO edits VALUES ('a', 1, 1, 'enwiki', 's', 'edit', 'T', 'u', 0, 0, 0, 1, 2);")
    old.commit(); old.close()
    monkeypatch.setattr(config, "DB_PATH", path)
    con = listener.open_db()
    cols = {r[1] for r in con.execute("PRAGMA table_info(edits)")}
    assert {"rev_new", "rev_old", "comment"} <= cols
    assert con.execute("SELECT COUNT(*) FROM edits").fetchone()[0] == 1       # old rows kept
    listener.open_db().close()                                                  # second open is fine