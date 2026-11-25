import argparse
import logging
import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from wikeds import config
from wikeds.summary_store import open_store

log = logging.getLogger("wiki-rollup")

HOUR = 3600
TIMEZONE = config.TIMEZONE       # hours (and days) are aligned to this zone
CHUNK_HOURS = 24                 # aggregate this many hours per read/write
SUMMARY_RETENTION_DAYS = 365

# raw events live in local sqlite, so this always runs against the raw connection
_AGGREGATE_SQL = """
SELECT ((event_time + :off) / 3600) * 3600 - :off AS hour_start,
       COALESCE(wiki, '')                    AS wiki,
       COALESCE(type, '')                    AS type,
       COALESCE(bot, 0)                      AS bot,
       COUNT(*)                              AS edits,
       COUNT(DISTINCT user)                  AS editors,
       COALESCE(SUM(CASE WHEN new_len IS NULL THEN 0
                         ELSE new_len - COALESCE(old_len, 0) END), 0) AS net_bytes
FROM edits
WHERE event_time >= :lo AND event_time < :hi
GROUP BY 1, 2, 3, 4
"""


def tz_offset_seconds(tz=TIMEZONE):
    """Current UTC offset of `tz` in seconds (19800 for Asia/Kolkata)."""
    return int(datetime.now(ZoneInfo(tz)).utcoffset().total_seconds())


def bucket_start(t, offset):
    """Start (UTC seconds) of the local-aligned hour containing time t."""
    return ((int(t) + offset) // HOUR) * HOUR - offset


def run_rollup(raw, store, now=None, grace_seconds=300, offset=None):
    """Summarize raw events (sqlite connection `raw`) into the summary store.

    grace_seconds: an hour is final only this long after it ends, which leaves
    room for slightly late events.
    offset: UTC offset in seconds for hour alignment; None = from TIMEZONE.
    Ignored once a summary exists, so old hours stay aligned.
    """
    now = int(time.time() if now is None else now)

    configured = tz_offset_seconds() if offset is None else int(offset)
    offset = store.get_meta("bucket_offset")
    if offset is None:
        offset = configured
        store.set_meta("bucket_offset", offset)
    elif offset != configured:
        log.warning("summary was built with UTC offset %ds but TIMEZONE gives %ds; "
                    "keeping %ds so existing hours stay aligned", offset, configured, offset)

    mn, _ = raw.execute("SELECT MIN(event_time), MAX(event_time) FROM edits").fetchone()
    if mn is None:
        return {"rows_written": 0, "start": None, "end": None, "rolled_through": None,
                "offset": offset}

    raw_first_hour = bucket_start(mn, offset)
    rolled = store.get_meta("rolled_through")
    if rolled is None:
        start = raw_first_hour
    elif raw_first_hour > rolled:
        # raw data before this point is gone (cloud restart?) and was never rolled up
        log.warning("gap in summary: raw data starts at hour %d, last rolled hour was %d",
                    raw_first_hour, rolled)
        start = raw_first_hour
    else:
        start = rolled

    end = bucket_start(now, offset) + HOUR    # end of the current (partial) hour
    rows_written = 0
    t = start
    while t < end:
        t_end = min(t + CHUNK_HOURS * HOUR, end)
        rows = raw.execute(_AGGREGATE_SQL, {"off": offset, "lo": t, "hi": t_end}).fetchall()
        store.upsert_hours(rows)   # never shrinks an hour that's already stored
        rows_written += len(rows)
        t = t_end

    complete_end = bucket_start(now - grace_seconds, offset)  # every hour before this is final
    new_rolled = max(complete_end, rolled if rolled is not None else start)
    store.set_meta("rolled_through", new_rolled)

    return {"rows_written": rows_written, "start": start, "end": end,
            "rolled_through": new_rolled, "offset": offset}


def prune_summary(store, now=None, retention_days=SUMMARY_RETENTION_DAYS):
    """Drop summary rows older than the retention period. Returns rows deleted."""
    now = int(time.time() if now is None else now)
    return store.prune(now - retention_days * 86400)


def _main():
    ap = argparse.ArgumentParser(description="Roll raw events up into the hourly summary.")
    ap.add_argument("--db", default=config.DB_PATH, help="raw events sqlite file")
    args = ap.parse_args()
    config.setup_logging()

    raw = sqlite3.connect(args.db, timeout=30)
    raw.execute("PRAGMA journal_mode=WAL;")
    store = open_store()
    t0 = time.time()
    try:
        stats = run_rollup(raw, store)
        pruned = prune_summary(store)
        print(f"rollup to {store.label} done in {time.time() - t0:.1f}s: "
              f"wrote {stats['rows_written']:,} rows, pruned {pruned:,}")
    finally:
        raw.close()
        store.close()


if __name__ == "__main__":
    _main()