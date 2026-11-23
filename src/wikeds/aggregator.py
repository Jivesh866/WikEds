import logging
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import pandas as pd

log = logging.getLogger("wiki-aggregator")

DB_PATH = "wiki.db"
WINDOW_HOURS = 3          # how much recent raw data to keep in memory
OVERLAP_SECONDS = 120     # re-read this much on each refresh to catch late events
LOCAL_TZ = "Asia/Kolkata"
HISTORY_DAYS = 30         # how much of summary_hourly to keep in memory
HISTORY_REFRESH_SECONDS = 60   # history changes slowly, so reload it at most this often

_COLUMNS = [
    "event_id", "event_time", "ingested_at", "wiki", "server_name", "type",
    "title", "user", "bot", "minor", "namespace", "old_len", "new_len",
]
_COLS_SQL = ", ".join(f'"{c}"' for c in _COLUMNS)

# Bytes added (+) or removed (-) by an event; 0 when the new length is unknown.
_DELTA = "CASE WHEN new_len IS NULL THEN 0 ELSE new_len - coalesce(old_len, 0) END"


class Aggregator:
    def __init__(self, db_path=DB_PATH, window_hours=WINDOW_HOURS, tz=LOCAL_TZ,
                 history_days=HISTORY_DAYS):
        if not Path(db_path).exists():
            raise FileNotFoundError(
                f"{db_path} not found. Start listener.py first so it creates the database."
            )
        self.db_path = db_path
        self.window_seconds = int(window_hours * 3600)
        self.history_days = int(history_days)
        self._hist_checked = 0.0
        self._bucket_offset = None   # UTC offset the summary hours are aligned to (from summary_meta)
        self.tz = tz
        self._lock = threading.Lock()  # one DuckDB connection shared across threads

        self.con = duckdb.connect(":memory:")
        self.con.execute("""
            CREATE TABLE recent (
                event_id    VARCHAR,
                event_time  BIGINT,
                ingested_at BIGINT,
                wiki        VARCHAR,
                server_name VARCHAR,
                type        VARCHAR,
                title       VARCHAR,
                "user"      VARCHAR,
                bot         INTEGER,
                minor       INTEGER,
                namespace   INTEGER,
                old_len     BIGINT,
                new_len     BIGINT
            )
        """)
        self.con.execute("""
            CREATE TABLE hist (
                hour_start BIGINT,
                wiki       VARCHAR,
                type       VARCHAR,
                bot        INTEGER,
                edits      BIGINT,
                editors    BIGINT,
                net_bytes  BIGINT
            )
        """)
        self.refresh()

    # ------------------------------------------------------------------ loading
    def _read_sqlite(self, since):
        con = sqlite3.connect(self.db_path, timeout=30)
        try:
            con.execute("PRAGMA query_only = ON;")
            df = pd.read_sql_query(
                f"SELECT {_COLS_SQL} FROM edits WHERE event_time >= ?", con, params=(since,)
            )
        finally:
            con.close()
        # nullable ints so missing lengths become NULL, not NaN
        for col in ("namespace", "old_len", "new_len"):
            df[col] = df[col].astype("Int64")
        return df

    def refresh(self):
        """Pull new raw rows (and, at most once a minute, new summary rows).

        Returns the number of raw rows read from SQLite (including overlap rows
        that were already cached and therefore skipped).
        """
        n = self._refresh_recent()
        try:
            self.refresh_history()
        except sqlite3.Error:
            log.warning("could not refresh history from summary_hourly", exc_info=True)
        return n

    def _refresh_recent(self):
        with self._lock:
            cutoff = int(time.time()) - self.window_seconds
            last = self.con.execute("SELECT max(event_time) FROM recent").fetchall()[0][0]
            since = cutoff if last is None else max(cutoff, last - OVERLAP_SECONDS)

            df = self._read_sqlite(since)
            if len(df):
                self.con.register("incoming", df)
                self.con.execute(f"""
                    INSERT INTO recent
                    SELECT {_COLS_SQL} FROM incoming i
                    WHERE NOT EXISTS (SELECT 1 FROM recent r WHERE r.event_id = i.event_id)
                """)
                self.con.unregister("incoming")
            self.con.execute("DELETE FROM recent WHERE event_time < ?", [cutoff])
            return len(df)

    # ---------------------------------------------------------------- internals
    def _df(self, sql, params=None):
        with self._lock:
            return self.con.execute(sql, params or []).df()

    def _one(self, sql, params=None):
        # fetchall (not fetchone) so the result is fully consumed and the
        # connection is free for the next call
        with self._lock:
            return self.con.execute(sql, params or []).fetchall()[0]

    def _since(self, minutes):
        """Clamp minutes to the cached window and return (unix cutoff, minutes)."""
        minutes = max(1, min(int(minutes), self.window_seconds // 60))
        return int(time.time()) - minutes * 60, minutes

    @staticmethod
    def _filters(include_bots=True, wikis=None):
        """Optional extra WHERE clauses shared by most metrics."""
        sql, params = "", []
        if not include_bots:
            sql += " AND coalesce(bot, 0) = 0"
        if wikis:
            sql += " AND list_contains(?, wiki)"
            params.append(list(wikis))
        return sql, params

    def _to_local(self, df, col):
        df[col] = pd.to_datetime(df[col], unit="s", utc=True).dt.tz_convert(self.tz)
        return df

    # ------------------------------------------------------------------ metrics
    def health(self):
        """Pipeline health: is the listener still feeding data?"""
        n, last_event, last_ingest = self._one(
            "SELECT count(*), max(event_time), max(ingested_at) FROM recent"
        )
        now = int(time.time())
        return {
            "rows_cached": n,
            "newest_event_age_s": None if last_event is None else now - last_event,
            "newest_ingest_age_s": None if last_ingest is None else now - last_ingest,
            "window_hours": self.window_seconds / 3600,
        }

    def headline(self, minutes=15, include_bots=True, wikis=None):
        """Big-number tiles: edits, rate, bot share, active wikis, editors."""
        since, minutes = self._since(minutes)
        f, fp = self._filters(include_bots, wikis)
        row = self._df(f"""
            SELECT count(*) AS edits,
                   count(DISTINCT wiki) AS active_wikis,
                   count(DISTINCT "user") AS editors,
                   coalesce(CAST(sum(CASE WHEN bot = 1 THEN 1 ELSE 0 END) AS BIGINT), 0) AS bot_edits
            FROM recent
            WHERE event_time >= ? {f}
        """, [since, *fp]).iloc[0]
        edits = int(row["edits"])
        return {
            "minutes": minutes,
            "edits": edits,
            "edits_per_min": round(edits / minutes, 1),
            "active_wikis": int(row["active_wikis"]),
            "editors": int(row["editors"]),
            "bot_pct": round(100.0 * int(row["bot_edits"]) / edits, 1) if edits else 0.0,
        }

    def edits_per_minute(self, minutes=60, include_bots=True, wikis=None, smooth=5):
        """Gap-filled per-minute series with a bot/human split and rolling average.

        The current (incomplete) minute is left out so the last point doesn't dip.
        Columns: minute (local tz), edits, bots, humans, edits_avg.
        """
        _, minutes = self._since(minutes)
        smooth = max(1, int(smooth))
        f, fp = self._filters(include_bots, wikis)
        hi = (int(time.time()) // 60) * 60 - 60      # last complete minute
        lo = hi - (minutes - 1) * 60
        df = self._df(f"""
            WITH grid AS (
                SELECT bucket FROM generate_series(CAST(? AS BIGINT), CAST(? AS BIGINT), 60) AS g(bucket)
            ),
            agg AS (
                SELECT (event_time // 60) * 60 AS bucket,
                       count(*) AS edits,
                       CAST(sum(CASE WHEN bot = 1 THEN 1 ELSE 0 END) AS BIGINT) AS bots
                FROM recent
                WHERE event_time >= ? AND event_time < ? {f}
                GROUP BY 1
            )
            SELECT grid.bucket AS minute,
                   coalesce(agg.edits, 0) AS edits,
                   coalesce(agg.bots, 0) AS bots,
                   coalesce(agg.edits, 0) - coalesce(agg.bots, 0) AS humans,
                   round(avg(coalesce(agg.edits, 0)) OVER (
                       ORDER BY grid.bucket ROWS BETWEEN {smooth - 1} PRECEDING AND CURRENT ROW
                   ), 1) AS edits_avg
            FROM grid LEFT JOIN agg USING (bucket)
            ORDER BY grid.bucket
        """, [lo, hi, lo, hi + 60, *fp])
        return self._to_local(df, "minute")

    def top_wikis(self, minutes=15, limit=10, include_bots=True):
        """Busiest wikis: edits, bot share, distinct editors."""
        since, _ = self._since(minutes)
        f, fp = self._filters(include_bots)
        return self._df(f"""
            SELECT wiki,
                   count(*) AS edits,
                   CAST(sum(CASE WHEN bot = 1 THEN 1 ELSE 0 END) AS BIGINT) AS bot_edits,
                   round(100.0 * sum(CASE WHEN bot = 1 THEN 1 ELSE 0 END) / count(*), 1) AS bot_pct,
                   count(DISTINCT "user") AS editors
            FROM recent
            WHERE event_time >= ? {f}
            GROUP BY wiki
            ORDER BY edits DESC, wiki
            LIMIT ?
        """, [since, *fp, int(limit)])

    def top_pages(self, minutes=15, limit=10, include_bots=False, wikis=None):
        """Most-edited pages right now (edits and page creations only)."""
        since, _ = self._since(minutes)
        f, fp = self._filters(include_bots, wikis)
        df = self._df(f"""
            SELECT wiki, title,
                   count(*) AS edits,
                   count(DISTINCT "user") AS editors,
                   CAST(sum({_DELTA}) AS BIGINT) AS net_bytes,
                   max(event_time) AS last_edit
            FROM recent
            WHERE event_time >= ? AND type IN ('edit', 'new') {f}
            GROUP BY wiki, title
            ORDER BY edits DESC, last_edit DESC
            LIMIT ?
        """, [since, *fp, int(limit)])
        return self._to_local(df, "last_edit")

    def biggest_edits(self, minutes=60, limit=10, include_bots=True, wikis=None):
        """Largest single changes by bytes added or removed."""
        since, _ = self._since(minutes)
        f, fp = self._filters(include_bots, wikis)
        df = self._df(f"""
            SELECT event_time AS edit_time, wiki, title, "user",
                   (bot = 1) AS bot,
                   CAST({_DELTA} AS BIGINT) AS bytes_changed
            FROM recent
            WHERE event_time >= ? AND type IN ('edit', 'new') AND new_len IS NOT NULL {f}
            ORDER BY abs({_DELTA}) DESC, event_time DESC
            LIMIT ?
        """, [since, *fp, int(limit)])
        return self._to_local(df, "edit_time")

    def by_language(self, minutes=15, limit=10, include_bots=True):
        """Wikipedia language editions only (e.g. en.wikipedia.org -> 'en')."""
        since, _ = self._since(minutes)
        f, fp = self._filters(include_bots)
        return self._df(f"""
            SELECT split_part(server_name, '.', 1) AS language,
                   count(*) AS edits,
                   CAST(sum(CASE WHEN bot = 1 THEN 1 ELSE 0 END) AS BIGINT) AS bot_edits,
                   count(DISTINCT "user") AS editors
            FROM recent
            WHERE event_time >= ? AND server_name LIKE '%.wikipedia.org' {f}
            GROUP BY 1
            ORDER BY edits DESC, language
            LIMIT ?
        """, [since, *fp, int(limit)])

    def by_type(self, minutes=15, include_bots=True, wikis=None):
        """Event mix: edit / new / log / categorize ..."""
        since, _ = self._since(minutes)
        f, fp = self._filters(include_bots, wikis)
        return self._df(f"""
            SELECT type, count(*) AS events
            FROM recent
            WHERE event_time >= ? {f}
            GROUP BY type
            ORDER BY events DESC, type
        """, [since, *fp])

    def recent_events(self, limit=25, include_bots=True, wikis=None):
        """Latest events, newest first (for the live table)."""
        f, fp = self._filters(include_bots, wikis)
        df = self._df(f"""
            SELECT event_time AS edit_time, wiki, type, title, "user",
                   (bot = 1) AS bot,
                   CAST({_DELTA} AS BIGINT) AS bytes_changed
            FROM recent
            WHERE true {f}
            ORDER BY event_time DESC, event_id
            LIMIT ?
        """, [*fp, int(limit)])
        return self._to_local(df, "edit_time")

    def available_wikis(self, minutes=60, limit=50):
        """Wiki names active recently, busiest first (for a sidebar filter)."""
        since, _ = self._since(minutes)
        df = self._df("""
            SELECT wiki, count(*) AS edits
            FROM recent
            WHERE event_time >= ? AND wiki IS NOT NULL
            GROUP BY wiki
            ORDER BY edits DESC, wiki
            LIMIT ?
        """, [since, int(limit)])
        return df["wiki"].tolist()

    # ------------------------------------------------------------------ history
    def _read_summary(self, since_hour):
        cols = ["hour_start", "wiki", "type", "bot", "edits", "editors", "net_bytes"]
        con = sqlite3.connect(self.db_path, timeout=30)
        try:
            con.execute("PRAGMA query_only = ON;")
            exists = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'summary_hourly'"
            ).fetchone()
            if not exists:  # database created before rollups existed
                return pd.DataFrame(columns=cols)
            row = con.execute(
                "SELECT value FROM summary_meta WHERE key = 'bucket_offset'"
            ).fetchone()
            if row is not None:
                self._bucket_offset = int(row[0])
            return pd.read_sql_query(
                f"SELECT {', '.join(cols)} FROM summary_hourly WHERE hour_start >= ?",
                con, params=(since_hour,),
            )
        finally:
            con.close()

    def refresh_history(self, force=False):
        """Load new or changed rows of summary_hourly into memory.

        Hours are final once rolled up, except the newest couple, which rollup.py
        keeps recomputing, so only hours from (newest cached - 2h) on are re-read.
        Throttled to once per HISTORY_REFRESH_SECONDS unless force=True.
        Returns the number of summary rows read.
        """
        now = time.time()
        if not force and now - self._hist_checked < HISTORY_REFRESH_SECONDS:
            return 0
        with self._lock:
            cutoff = (int(now) // 3600) * 3600 - self.history_days * 86400
            last = self.con.execute("SELECT max(hour_start) FROM hist").fetchall()[0][0]
            since = cutoff if last is None else max(cutoff, last - 2 * 3600)
            df = self._read_summary(since)
            self.con.execute("DELETE FROM hist WHERE hour_start >= ?", [since])
            if len(df):
                self.con.register("incoming_hist", df)
                self.con.execute("""
                    INSERT INTO hist
                    SELECT hour_start, wiki, type, bot, edits, editors, net_bytes FROM incoming_hist
                """)
                self.con.unregister("incoming_hist")
            self.con.execute("DELETE FROM hist WHERE hour_start < ?", [cutoff])
            self._hist_checked = now
            return len(df)

    def _utc_offset(self):
        """UTC offset (seconds) that days and hours are cut on.

        This is the offset the summary hours were aligned to when rollup.py built
        them (so a "day" is exactly one local calendar day); before any summary
        exists it falls back to the current offset of the display timezone.
        """
        if self._bucket_offset is not None:
            return self._bucket_offset
        return int(datetime.now(ZoneInfo(self.tz)).utcoffset().total_seconds())

    def _hist_range(self, days):
        """(first hour, last complete hour) covering `days` days; the in-progress hour is left out.

        Hour starts are aligned to the local clock, which for India means :30 past
        the hour in UTC, so the range is built from the aligned current hour.
        """
        days = max(1, min(int(days), self.history_days))
        off = self._utc_offset()
        hi = ((int(time.time()) + off) // 3600) * 3600 - off - 3600
        return hi - (days * 24 - 1) * 3600, hi

    def history_coverage(self):
        """What the summary covers: first/last hour (local tz), hours with data, hours in span."""
        n, first, last = self._one(
            "SELECT count(DISTINCT hour_start), min(hour_start), max(hour_start) FROM hist"
        )
        if not n:
            return {"hours_with_data": 0, "first_hour": None, "last_hour": None, "hours_in_span": 0}
        to_local = lambda s: pd.Timestamp(int(s), unit="s", tz="UTC").tz_convert(self.tz)
        return {
            "hours_with_data": int(n),
            "first_hour": to_local(first),
            "last_hour": to_local(last),
            "hours_in_span": int((last - first) // 3600 + 1),
        }

    def history_hourly(self, days=7, include_bots=True, wikis=None):
        """Edits per hour over the last `days` days (last complete hour is the end).

        Hours with no summary rows at all (the listener was down) come back as
        NaN so charts show a gap instead of a misleading zero.
        Columns: hour (local tz), edits, bots, humans.
        """
        lo, hi = self._hist_range(days)
        f, fp = self._filters(include_bots, wikis)
        df = self._df(f"""
            WITH grid AS (
                SELECT hour_start FROM generate_series(CAST(? AS BIGINT), CAST(? AS BIGINT), 3600) AS g(hour_start)
            ),
            agg AS (
                SELECT hour_start,
                       CAST(sum(edits) AS BIGINT) AS edits,
                       CAST(sum(CASE WHEN bot = 1 THEN edits ELSE 0 END) AS BIGINT) AS bots
                FROM hist
                WHERE hour_start >= ? AND hour_start <= ? {f}
                GROUP BY 1
            ),
            present AS (
                SELECT DISTINCT hour_start FROM hist WHERE hour_start >= ? AND hour_start <= ?
            )
            SELECT grid.hour_start AS hour,
                   CASE WHEN present.hour_start IS NULL THEN NULL ELSE coalesce(agg.edits, 0) END AS edits,
                   CASE WHEN present.hour_start IS NULL THEN NULL ELSE coalesce(agg.bots, 0) END AS bots,
                   CASE WHEN present.hour_start IS NULL THEN NULL
                        ELSE coalesce(agg.edits, 0) - coalesce(agg.bots, 0) END AS humans
            FROM grid
            LEFT JOIN present USING (hour_start)
            LEFT JOIN agg USING (hour_start)
            ORDER BY grid.hour_start
        """, [lo, hi, lo, hi, *fp, lo, hi])
        for col in ("edits", "bots", "humans"):
            df[col] = df[col].astype("float64")   # gap hours become NaN (plain float, plot-friendly)
        return self._to_local(df, "hour")

    def history_daily(self, days=30, include_bots=True, wikis=None):
        """One row per local calendar day, today included (a partial day).

        `hours_covered` says how many of the day's hours have data, so a short
        bar can be told apart from a quiet day. Days with no data are NaN.
        Columns: day, edits, humans, bots, bot_pct, active_wikis, net_bytes, hours_covered.
        """
        days = max(1, min(int(days), self.history_days))
        off = self._utc_offset()
        now = int(time.time())
        today = (now + off) // 86400
        first = today - (days - 1)
        lo = first * 86400 - off              # local midnight of the first day, in UTC seconds
        hi = now                              # up to and including the current (partial) hour
        f, fp = self._filters(include_bots, wikis)

        df = self._df(f"""
            SELECT (hour_start + ?) // 86400 AS day,
                   CAST(sum(edits) AS BIGINT) AS edits,
                   CAST(sum(CASE WHEN bot = 1 THEN edits ELSE 0 END) AS BIGINT) AS bots,
                   count(DISTINCT CASE WHEN wiki <> '' THEN wiki END) AS active_wikis,
                   CAST(sum(net_bytes) AS BIGINT) AS net_bytes
            FROM hist
            WHERE hour_start >= ? AND hour_start <= ? {f}
            GROUP BY 1
        """, [off, lo, hi, *fp])
        cov = self._df("""
            SELECT (hour_start + ?) // 86400 AS day, count(DISTINCT hour_start) AS hours_covered
            FROM hist WHERE hour_start >= ? AND hour_start <= ? GROUP BY 1
        """, [off, lo, hi])

        out = pd.DataFrame({"day": range(first, today + 1)})
        out = out.merge(df, on="day", how="left").merge(cov, on="day", how="left")
        out["hours_covered"] = out["hours_covered"].fillna(0).astype(int)
        covered = out["hours_covered"] > 0      # data exists, the filter just matched nothing -> 0
        for c in ("edits", "bots", "active_wikis", "net_bytes"):
            out.loc[covered, c] = out.loc[covered, c].fillna(0)
        out["humans"] = out["edits"] - out["bots"]
        out["bot_pct"] = (100.0 * out["bots"] / out["edits"]).round(1)
        out["day"] = pd.to_datetime(out["day"] * 86400, unit="s")   # local midnight, tz-naive
        return out[["day", "edits", "humans", "bots", "bot_pct", "active_wikis",
                    "net_bytes", "hours_covered"]]

    def history_top_wikis(self, days=7, limit=10, include_bots=True):
        """Busiest wikis over the last `days` days: edits and bot share."""
        lo, hi = self._hist_range(days)
        f, fp = self._filters(include_bots)
        return self._df(f"""
            SELECT wiki,
                   CAST(sum(edits) AS BIGINT) AS edits,
                   CAST(sum(CASE WHEN bot = 1 THEN edits ELSE 0 END) AS BIGINT) AS bot_edits,
                   round(100.0 * sum(CASE WHEN bot = 1 THEN edits ELSE 0 END) / sum(edits), 1) AS bot_pct
            FROM hist
            WHERE hour_start >= ? AND hour_start <= ? AND wiki <> '' {f}
            GROUP BY wiki
            ORDER BY edits DESC, wiki
            LIMIT ?
        """, [lo, hi, *fp, int(limit)])

    def history_hour_of_day(self, days=7, include_bots=True, wikis=None):
        """Average edits per hour for each local hour of the day (0-23).

        Averages only hours that have data, so listener downtime doesn't drag
        the profile down. Columns: hour_of_day, avg_edits, avg_bots, hours_observed.
        """
        lo, hi = self._hist_range(days)
        off = self._utc_offset()
        f, fp = self._filters(include_bots, wikis)
        df = self._df(f"""
            SELECT hod AS hour_of_day,
                   CAST(round(avg(h_edits), 0) AS BIGINT) AS avg_edits,
                   CAST(round(avg(h_bots), 0) AS BIGINT) AS avg_bots,
                   count(*) AS hours_observed
            FROM (
                SELECT hour_start, ((hour_start + ?) // 3600) % 24 AS hod,
                       sum(edits) AS h_edits,
                       sum(CASE WHEN bot = 1 THEN edits ELSE 0 END) AS h_bots
                FROM hist
                WHERE hour_start >= ? AND hour_start <= ? {f}
                GROUP BY 1, 2
            )
            GROUP BY hod
            ORDER BY hod
        """, [off, lo, hi, *fp])
        grid = pd.DataFrame({"hour_of_day": range(24)})
        return grid.merge(df, on="hour_of_day", how="left")


# ------------------------------------------------------------------ CLI check
def _timed(label, fn):
    t0 = time.perf_counter()
    out = fn()
    ms = (time.perf_counter() - t0) * 1000
    print(f"\n== {label}  ({ms:.1f} ms)")
    if isinstance(out, pd.DataFrame):
        print(out.to_string(index=False) if len(out) else "(no rows)")
    else:
        print(out)


def _main():
    import argparse

    ap = argparse.ArgumentParser(description="Print every metric from wiki.db using DuckDB.")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--window-hours", type=float, default=WINDOW_HOURS)
    ap.add_argument("--watch", type=int, metavar="SECONDS",
                    help="keep refreshing and print the headline every N seconds")
    args = ap.parse_args()

    t0 = time.perf_counter()
    agg = Aggregator(args.db, window_hours=args.window_hours)
    h = agg.health()
    print(f"loaded {h['rows_cached']:,} rows in {time.perf_counter() - t0:.2f}s")

    _timed("health", agg.health)
    _timed("headline (15 min)", lambda: agg.headline(15))
    _timed("edits per minute (last 15, all)", lambda: agg.edits_per_minute(15))
    _timed("top wikis (15 min)", lambda: agg.top_wikis(15))
    _timed("top pages (15 min, humans only)", lambda: agg.top_pages(15))
    _timed("biggest edits (60 min)", lambda: agg.biggest_edits(60))
    _timed("by language (15 min)", lambda: agg.by_language(15))
    _timed("by type (15 min)", lambda: agg.by_type(15))
    _timed("recent events", lambda: agg.recent_events(10))
    _timed("history coverage", agg.history_coverage)
    _timed("history daily (7 days)", lambda: agg.history_daily(7))
    _timed("history top wikis (7 days)", lambda: agg.history_top_wikis(7))

    if args.watch:
        print(f"\nwatching, refreshing every {args.watch}s (Ctrl+C to stop)")
        try:
            while True:
                time.sleep(args.watch)
                t = time.perf_counter()
                new = agg.refresh()
                print(f"refresh read {new} rows in {(time.perf_counter() - t) * 1000:.0f} ms | "
                      f"{agg.headline(5)} | {agg.health()}")
        except KeyboardInterrupt:
            print("stopped")


if __name__ == "__main__":
    _main()