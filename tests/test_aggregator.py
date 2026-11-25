import time

import pytest

pytest.importorskip("duckdb")

from wikeds import config, listener
from wikeds.aggregator import Aggregator


def test_events_frame_and_top_editors(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "w.db"))
    raw = listener.open_db()
    now = int(time.time())

    def r(i, t, user, bot, title, new):
        return (f"id{i}", t, t, "enwiki", "en.wikipedia.org", "edit", title, user, bot, 0, 0, 100, new)

    raw.executemany(listener.INSERT_SQL, [
        r(0, now - 10, "alice", 0, "A", 160), r(1, now - 20, "alice", 0, "B", 90),
        r(2, now - 30, "bob", 0, "A", 100), r(3, now - 40, "robot", 1, "C", 110),
    ])
    raw.commit()
    agg = Aggregator(config.DB_PATH)

    df = agg.events_frame(180)
    assert len(df) == 4 and {"server_name", "namespace", "old_len"} <= set(df.columns)

    te = agg.top_editors(15, 10, True).set_index("user")
    assert list(te.index)[0] == "alice"
    assert te.loc["alice", "edits"] == 2 and te.loc["alice", "pages"] == 2
    assert te.loc["alice", "net_bytes"] == 50           # +60 and -10
    assert bool(te.loc["robot", "bot"]) and not bool(te.loc["bob", "bot"])
    assert "robot" not in list(agg.top_editors(15, 10, False)["user"])