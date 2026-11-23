import argparse
import logging
import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger("wiki-rollup")

HOUR = 3600
TIMEZONE = "Asia/Kolkata"        # hours (and, in the dashboard, days) are aligned to this zone
CHUNK_HOURS = 24                 # aggregate this many hours per read/write
SUMMARY_RETENTION_DAYS = 365

SUMMARY_SCHEMA = """
CREATE TABLE IF NOT EXISTS summary_hourly (
    hour_start INTEGER NOT NULL,
    wiki       TEXT    NOT NULL,
    type       TEXT    NOT NULL,
    bot        INTEGER NOT NULL,
    edits      INTEGER NOT NULL,
    editors    INTEGER NOT NULL,
    net_bytes  INTEGER NOT NULL,
    PRIMARY KEY (hour_start, wiki, type, bot)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS summary_meta (
    key   TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
"""

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

_INSERT_SQL = """
INSERT OR REPLACE INTO summary_hourly
    (hour_start, wiki, type, bot, edits, editors, net_bytes)
VALUES (?, ?, ?, ?, ?, ?, ?)
"""


def ensure_schema(con):
    con.executescript(SUMMARY_SCHEMA)


def tz_offset_seconds(tz=TIMEZONE):
    """Current UTC offset of `tz` in seconds (19800 for Asia/Kolkata)."""
    return int(datetime.now(ZoneInfo(tz)).utcoffset().total_seconds())


def bucket_start(t, offset):
    """Start (UTC seconds) of the local-aligned hour containing time t."""
    return ((int(t) + offset) // HOUR) * HOUR - offset


def get_bucket_offset(con):
    """The offset the stored summary was built with, or None for a new summary."""
    return _get_meta(con, "bucket_offset")


def _get_meta(con, key):
    row = con.execute("SELECT value FROM summary_meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else row[0]


def _set_meta(con, key, value):
    con.execute(
        "INSERT INTO summary_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, int(value)),
    )


def run_rollup(con, now=None, grace_seconds=300, offset=None):
    """Summarize raw events into summary_hourly. Returns a stats dict.

    grace_seconds: an hour counts as final only this long after it ends, which
    leaves room for events that arrive slightly late.
    offset: UTC offset in seconds for hour alignment; None means "from TIMEZONE"
    (ignored once a summary exists, see "Time zone" above).
    """
    ensure_schema(con)
    now = int(time.time() if now is None else now)

    configured = tz_offset_seconds() if offset is None else int(offset)
    offset = get_bucket_offset(con)
    if offset is None:
        offset = configured
        with con:
            _set_meta(con, "bucket_offset", offset)
    elif offset != configured:
        log.warning("summary was built with UTC offset %ds but TIMEZONE gives %ds; "
                    "keeping %ds so existing hours stay aligned", offset, configured, offset)

    mn, mx = con.execute("SELECT MIN(event_time), MAX(event_time) FROM edits").fetchone()
    if mn is None:
        return {"rows_written": 0, "start": None, "end": None, "rolled_through": None,
                "offset": offset}

    raw_first_hour = bucket_start(mn, offset)
    rolled = _get_meta(con, "rolled_through")
    if rolled is None:
        start = raw_first_hour
    elif raw_first_hour > rolled:
        # raw data before this point is gone and was never rolled up
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
        rows = con.execute(_AGGREGATE_SQL, {"off": offset, "lo": t, "hi": t_end}).fetchall()  # read: no write lock
        with con:                                                   # short write
            con.execute("DELETE FROM summary_hourly WHERE hour_start >= ? AND hour_start < ?",
                        (t, t_end))
            con.executemany(_INSERT_SQL, rows)
        rows_written += len(rows)
        t = t_end

    complete_end = bucket_start(now - grace_seconds, offset)  # every hour before this is final
    new_rolled = max(complete_end, rolled if rolled is not None else start)
    with con:
        _set_meta(con, "rolled_through", new_rolled)

    return {"rows_written": rows_written, "start": start, "end": end,
            "rolled_through": new_rolled, "offset": offset}


def prune_summary(con, now=None, retention_days=SUMMARY_RETENTION_DAYS):
    """Drop summary rows older than the retention period. Returns rows deleted."""
    now = int(time.time() if now is None else now)
    cutoff = now - retention_days * 86400
    with con:
        cur = con.execute("DELETE FROM summary_hourly WHERE hour_start < ?", (cutoff,))
    return cur.rowcount


def _main():
    ap = argparse.ArgumentParser(description="Roll raw events up into summary_hourly.")
    ap.add_argument("--db", default="wiki.db")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    con = sqlite3.connect(args.db, timeout=30)
    con.execute("PRAGMA journal_mode=WAL;")
    t0 = time.time()
    stats = run_rollup(con)
    pruned = prune_summary(con)
    n = con.execute("SELECT COUNT(*) FROM summary_hourly").fetchone()[0]
    print(f"rollup done in {time.time() - t0:.1f}s: wrote {stats['rows_written']:,} rows, "
          f"pruned {pruned:,}; summary_hourly now has {n:,} rows")
    con.close()


if __name__ == "__main__":
    _main()