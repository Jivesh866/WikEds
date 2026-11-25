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
    assert row[11:] == (100, 150)


def test_missing_length_becomes_none():
    row = parse_event(_event(length=None))
    assert row[11] is None and row[12] is None


def test_junk_events_are_skipped():
    assert parse_event({"timestamp": 1}) is None            # no meta id
    assert parse_event(_event(timestamp=None)) is None      # no timestamp
    assert parse_event({}) is None