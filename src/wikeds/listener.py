import json
import logging
import signal
import sqlite3
import threading
import time
from logging.handlers import RotatingFileHandler

import requests

import rollup

# ---------- config ----------
URL = "https://stream.wikimedia.org/v2/stream/recentchange"
USER_AGENT = "wiki-edit-monitor/0.1 (your-email@example.com)"  # change this
DB_PATH = "wiki.db"
BATCH_SIZE = 200            # flush after this many events...
FLUSH_SECONDS = 5           # ...or this many seconds, whichever comes first
RETENTION_DAYS = 7          # delete raw rows older than this (after they are rolled up)
CLEANUP_EVERY_SECONDS = 600 # how often the maintenance job (rollup + cleanup) runs
DELETE_CHUNK = 50_000       # rows per delete transaction, keeps write locks short
STATS_EVERY_SECONDS = 60
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 60           # no data for this long -> treat stream as stalled, reconnect
MAX_BACKOFF = 60

# ---------- logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler("listener.log", maxBytes=5_000_000, backupCount=3),
    ],
)
log = logging.getLogger("wiki-listener")

# ---------- database ----------
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
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    con.executescript(SCHEMA)
    rollup.ensure_schema(con)
    return con


# ---------- parsing ----------
def parse_event(event):
    """Return a row tuple, or None if the event isn't usable."""
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


# ---------- listener ----------
class Listener:
    def __init__(self):
        self.con = open_db()
        self.buffer = []
        self.stop = False
        self.last_flush = time.time()
        self.last_cleanup = 0.0
        self.maint_thread = None
        self.last_stats = time.time()
        self.seen = 0        # events received since last stats line
        self.skipped = 0     # malformed/unusable events since last stats line
        self.written = 0     # rows flushed since last stats line

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
            log.exception("database write failed; keeping %d rows buffered", len(self.buffer))
            # keep the buffer so we can retry on the next flush, but cap it
            if len(self.buffer) > 50 * BATCH_SIZE:
                log.error("buffer too large, dropping oldest rows")
                self.buffer = self.buffer[-10 * BATCH_SIZE:]
        self.last_flush = time.time()

    def maybe_maintenance(self):
        """Start the rollup + cleanup job in a background thread when it is due."""
        now = time.time()
        if now - self.last_cleanup < CLEANUP_EVERY_SECONDS:
            return
        if self.maint_thread is not None and self.maint_thread.is_alive():
            return  # previous run still going
        self.last_cleanup = now
        self.maint_thread = threading.Thread(
            target=self._maintenance, name="maintenance", daemon=True
        )
        self.maint_thread.start()

    def _maintenance(self):
        """Roll raw events up into summary_hourly, then delete old raw rows.

        Runs in its own thread with its own connection so the stream is never
        blocked. The delete only happens if the rollup succeeded, so raw rows
        are never removed before they have been summarized.
        """
        con = None
        try:
            con = sqlite3.connect(DB_PATH, timeout=30)
            stats = rollup.run_rollup(con)
            log.info("rollup: wrote %d summary rows (final through hour %s)",
                     stats["rows_written"], stats["rolled_through"])

            pruned = rollup.prune_summary(con)
            if pruned:
                log.info("summary retention removed %d old rows", pruned)

            cutoff = int(time.time() - RETENTION_DAYS * 86400)
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
                log.info("retention cleanup removed %d old raw rows", deleted)
        except Exception:
            log.exception("maintenance failed; raw rows were kept")
        finally:
            if con is not None:
                con.close()

    def maybe_log_stats(self):
        now = time.time()
        if now - self.last_stats < STATS_EVERY_SECONDS:
            return
        log.info(
            "last %ds: received=%d written=%d skipped=%d buffered=%d",
            int(now - self.last_stats), self.seen, self.written, self.skipped, len(self.buffer),
        )
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
        """Connect and consume until the stream ends, errors, or stop is requested."""
        headers = {"User-Agent": USER_AGENT, "Accept": "text/event-stream"}
        with requests.get(
            URL, headers=headers, stream=True, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT)
        ) as resp:
            resp.raise_for_status()
            log.info("connected to %s", URL)
            for line in resp.iter_lines(decode_unicode=True):
                if self.stop:
                    return
                self.handle_line(line)
                if len(self.buffer) >= BATCH_SIZE or time.time() - self.last_flush >= FLUSH_SECONDS:
                    self.flush()
                self.maybe_maintenance()
                self.maybe_log_stats()

    def run(self):
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
                self.flush()  # never lose buffered rows on disconnect

            if self.stop:
                break
            # if we stayed connected for a while, reset the backoff
            if time.time() - started > 60:
                backoff = 1
            log.info("reconnecting in %ds", backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)

        self.flush()
        self.con.close()
        log.info("shut down cleanly")


if __name__ == "__main__":
    Listener().run()