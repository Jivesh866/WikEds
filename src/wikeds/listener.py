import json
import logging
import signal
import sqlite3
import threading
import time
from pathlib import Path

import requests

from wikeds import config, rollup
from wikeds.summary_store import open_store

log = logging.getLogger("wiki-listener")

DELETE_CHUNK = 50_000  # rows per delete, keeps write locks short

SCHEMA = """
CREATE TABLE IF NOT EXISTS edits (
    event_id    TEXT PRIMARY KEY,
    event_time  INTEGER NOT NULL,   -- unix seconds, UTC
    ingested_at INTEGER NOT NULL,   -- unix seconds, UTC
    wiki        TEXT,
    server_name TEXT,
    type        TEXT,
    title       TEXT,
    user        TEXT,
    bot         INTEGER,            -- 0/1
    minor       INTEGER,            -- 0/1
    namespace   INTEGER,
    old_len     INTEGER,
    new_len     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_edits_time ON edits (event_time);
CREATE INDEX IF NOT EXISTS idx_edits_wiki_time ON edits (wiki, event_time);
"""

INSERT_SQL = """
INSERT OR IGNORE INTO edits
    (event_id, event_time, ingested_at, wiki, server_name, type, title,
     user, bot, minor, namespace, old_len, new_len)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def open_db():
    Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(config.DB_PATH, timeout=30)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    con.executescript(SCHEMA)
    return con


def parse_event(event):
    """Row tuple, or None if the event is junk."""
    meta = event.get("meta") or {}
    event_id = meta.get("id")
    ts = event.get("timestamp")
    if not event_id or ts is None:
        return None
    length = event.get("length") or {}
    return (
        event_id,
        int(ts),
        int(time.time()),
        event.get("wiki"),
        event.get("server_name"),
        event.get("type"),
        event.get("title"),
        event.get("user"),
        int(bool(event.get("bot"))),
        int(bool(event.get("minor"))),
        event.get("namespace"),
        length.get("old"),
        length.get("new"),
    )


class Listener:
    def __init__(self):
        self.con = open_db()
        self.buffer = []
        self.stop = False
        self.last_flush = time.time()
        self.last_cleanup = 0.0
        self.maint_thread = None
        self.last_stats = time.time()
        self.seen = 0      # received since last stats line
        self.skipped = 0   # junk since last stats line
        self.written = 0   # flushed since last stats line

    def request_stop(self, *_):
        log.info("stop requested, finishing up")
        self.stop = True

    def flush(self):
        if not self.buffer:
            self.last_flush = time.time()
            return
        try:
            with self.con:  # one transaction per batch
                self.con.executemany(INSERT_SQL, self.buffer)
            self.written += len(self.buffer)
            self.buffer = []
        except sqlite3.Error:
            log.exception("db write failed, keeping %d rows buffered", len(self.buffer))
            # retry next flush, but don't grow forever
            if len(self.buffer) > 50 * config.BATCH_SIZE:
                log.error("buffer too big, dropping oldest rows")
                self.buffer = self.buffer[-10 * config.BATCH_SIZE:]
        self.last_flush = time.time()

    def maybe_maintenance(self):
        now = time.time()
        if now - self.last_cleanup < config.CLEANUP_EVERY_SECONDS:
            return
        if self.maint_thread is not None and self.maint_thread.is_alive():
            return  # last one still running
        self.last_cleanup = now
        self.maint_thread = threading.Thread(
            target=self._maintenance, name="maintenance", daemon=True
        )
        self.maint_thread.start()

    def _maintenance(self):
        """Rollup first, then delete old raw rows. Own thread + own connections."""
        con = store = None
        try:
            con = sqlite3.connect(config.DB_PATH, timeout=30)
            store = open_store()  # supabase or local, see config
            stats = rollup.run_rollup(con, store)
            log.info("rollup: wrote %d summary rows (final through %s)",
                     stats["rows_written"], stats["rolled_through"])

            pruned = rollup.prune_summary(store)
            if pruned:
                log.info("summary retention removed %d old rows", pruned)

            cutoff = int(time.time() - config.RETENTION_DAYS * 86400)
            deleted = 0
            while True:
                with con:
                    cur = con.execute(
                        "DELETE FROM edits WHERE rowid IN "
                        "(SELECT rowid FROM edits WHERE event_time < ? LIMIT ?)",
                        (cutoff, DELETE_CHUNK),
                    )
                deleted += cur.rowcount
                if cur.rowcount < DELETE_CHUNK:
                    break
            if deleted:
                log.info("cleanup removed %d old raw rows", deleted)
        except Exception:
            log.exception("maintenance failed, raw rows kept")
        finally:
            if con is not None:
                con.close()
            if store is not None:
                store.close()

    def maybe_log_stats(self):
        now = time.time()
        if now - self.last_stats < config.STATS_EVERY_SECONDS:
            return
        log.info("last %ds: received=%d written=%d skipped=%d buffered=%d",
                 int(now - self.last_stats), self.seen, self.written,
                 self.skipped, len(self.buffer))
        self.seen = self.written = self.skipped = 0
        self.last_stats = now

    def handle_line(self, line):
        if not line or not line.startswith("data: "):
            return
        self.seen += 1
        try:
            event = json.loads(line[6:])
        except json.JSONDecodeError:
            self.skipped += 1
            return
        row = parse_event(event)
        if row is None:
            self.skipped += 1
            return
        self.buffer.append(row)

    def stream_once(self):
        """Connect and read until the stream drops or we're told to stop."""
        headers = {"User-Agent": config.USER_AGENT, "Accept": "text/event-stream"}
        with requests.get(
            config.STREAM_URL, headers=headers, stream=True,
            timeout=(config.CONNECT_TIMEOUT, config.READ_TIMEOUT),
        ) as resp:
            resp.raise_for_status()
            log.info("connected to %s", config.STREAM_URL)
            for line in resp.iter_lines(decode_unicode=True):
                if self.stop:
                    return
                self.handle_line(line)
                if (len(self.buffer) >= config.BATCH_SIZE
                        or time.time() - self.last_flush >= config.FLUSH_SECONDS):
                    self.flush()
                self.maybe_maintenance()
                self.maybe_log_stats()

    def run(self, install_signals=True):
        # signal handlers only work on the main thread (not when embedded)
        if install_signals:
            signal.signal(signal.SIGINT, self.request_stop)
            signal.signal(signal.SIGTERM, self.request_stop)

        backoff = 1
        while not self.stop:
            started = time.time()
            try:
                self.stream_once()
            except (requests.RequestException, ConnectionError) as e:
                log.warning("stream error: %s", e)
            except Exception:
                log.exception("unexpected error in stream loop")
            finally:
                self.flush()  # don't lose buffered rows on disconnect

            if self.stop:
                break
            if time.time() - started > 60:
                backoff = 1  # we were connected a while, start fresh
            log.info("reconnecting in %ds", backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, config.MAX_BACKOFF)

        self.flush()
        self.con.close()
        log.info("shut down cleanly")


def start_in_thread():
    """Run the listener in a daemon thread. Used by the dashboard on Streamlit Cloud."""
    def _run():
        try:
            Listener().run(install_signals=False)
        except Exception:
            log.exception("embedded listener died")

    t = threading.Thread(target=_run, name="listener", daemon=True)
    t.start()
    return t


def main():
    config.setup_logging()
    Listener().run()


if __name__ == "__main__":
    main()