import time

import pytest

pytest.importorskip("duckdb")

from wikeds import config, listener, rollup
from wikeds.aggregator import Aggregator
from wikeds.summary_store import open_store


def test_ingest_health_and_summary_status(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "w.db"))
    raw = listener.open_db()
    now = int(time.time())
    # event happened 30s before we ingested it, 20 events arriving ~100-120s ago
    rows = [(f"id{i}", now - 130 - i, now - 100 - i, "enwiki", "en.wikipedia.org",
             "edit", "P", "u", 0, 0, 0, 10, 20) for i in range(20)]
    raw.executemany(listener.INSERT_SQL, rows)
    raw.commit()

    agg = Aggregator(config.DB_PATH)
    ing = agg.ingest_health(10)
    assert ing["per_min"] == 2 and ing["lag_p50"] == 30

    series = agg.ingest_per_minute(30)
    assert len(series) == 30 and series["events"].sum() == 20

    agg.refresh_history(force=True)
    ss = agg.summary_status()
    assert ss["error"] is None and ss["source"] == "local sqlite"
    assert ss["last_rollup_age_s"] is None            # nothing rolled up yet

    store = open_store()
    rollup.run_rollup(raw, store)
    store.close()
    agg.refresh_history(force=True)
    assert agg.summary_status()["last_rollup_age_s"] < 60