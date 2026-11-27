import json
import logging
import signal
import sqlite3
import threading
import time
from pathlib import Path

import requests

from wikeds import alerts, config, detector, flags, rollup
from wikeds.summary_store import open_store

log = logging.getLogger("wiki-listener")

DELETE_CHUNK = 50_000  # rows per delete, keeps write locks short

# what the background jobs last did, so the Health tab can show errors without digging in logs.
# only visible to the dashboard when the listener runs inside it (embedded / cloud mode)
STATUS = {"rollup": {}, "detect": {}}


def _note(job, ok, err=None, **info):
    now = time.time()
    last_ok = now if ok else STATUS[job].get("ok_at")     # when it last worked, for the watchdog
    STATUS[job] = {"at": now, "ok": ok, "err": err, "ok_at": last_ok, **info}


def _dur(seconds):
    s = int(max(seconds, 0))
    return f"{s} s" if s < 90 else f"{s // 60} min" if s < 7200 else f"{s // 3600} h"


def _err(e):
    first = (str(e).strip().splitlines() or [""])[0]
    return f"{type(e).__name__}: {first}"[:200]

FLAG_PRUNE_EVERY = 3600  # how often old flags are deleted
WATCHDOG_EVERY = 30      # seconds between outage checks

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
    new_len     INTEGER,
    rev_new     INTEGER,            -- revision ids, for diff links
    rev_old     INTEGER,
    comment     TEXT                -- edit summary, cut to 300 chars
);
CREATE INDEX IF NOT EXISTS idx_edits_time ON edits (event_time);
CREATE INDEX IF NOT EXISTS idx_edits_wiki_time ON edits (wiki, event_time);
"""

INSERT_SQL = """
INSERT OR IGNORE INTO edits
    (event_id, event_time, ingested_at, wiki, server_name, type, title,
     user, bot, minor, namespace, old_len, new_len, rev_new, rev_old, comment)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def open_db():
    Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(config.DB_PATH, timeout=30)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    con.executescript(SCHEMA)
    # a db made by an older version (local runs keep it) gets the newer columns
    have = {r[1] for r in con.execute("PRAGMA table_info(edits)")}
    for name, decl in (("rev_new", "INTEGER"), ("rev_old", "INTEGER"), ("comment", "TEXT")):
        if name not in have:
            try:
                con.execute(f"ALTER TABLE edits ADD COLUMN {name} {decl}")
            except sqlite3.OperationalError:
                pass   # another process added it a moment ago
    con.commit()
    return con


def parse_event(event):
    """Row tuple, or None if the event is junk."""
    meta = event.get("meta") or {}
    event_id = meta.get("id")
    ts = event.get("timestamp")
    if not event_id or ts is None:
        return None
    length = event.get("length") or {}
    rev = event.get("revision") or {}
    comment = (event.get("comment") or "")[:300] or None
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
        rev.get("new"),
        rev.get("old"),
        comment,
    )


class Listener:
    def __init__(self):
        self.con = open_db()
        self.buffer = []
        self.stop = False
        self.last_flush = time.time()
        self.last_cleanup = time.time() - config.CLEANUP_EVERY_SECONDS + 60   # first run after ~60 s
        self.maint_thread = None
        self.last_detect = time.time()   # first check after one interval
        self.detect_thread = None
        self.last_flag_prune = 0.0
        self.alert_retry = False   # last discord post failed, try again next check
        self.started_at = time.time()
        self.last_event_at = time.time()   # last line seen on the stream, for the stall check
        self.problems = {}                 # open outage -> (since, last message time)
        self.watchdog_thread = None
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
            _note("rollup", True, rows=stats["rows_written"], through=stats["rolled_through"])

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
        except Exception as e:
            _note("rollup", False, err=_err(e))
            log.exception("maintenance failed, raw rows kept")
        finally:
            if con is not None:
                con.close()
            if store is not None:
                store.close()

    def maybe_detect(self):
        if not config.DETECT_ENABLED:
            return
        now = time.time()
        if now - self.last_detect < config.DETECT_EVERY_SECONDS:
            return
        if self.detect_thread is not None and self.detect_thread.is_alive():
            return  # last check still running
        self.last_detect = now
        self.detect_thread = threading.Thread(
            target=self._detect, name="detector", daemon=True
        )
        self.detect_thread.start()

    def _detect(self):
        """Run the anomaly detectors and save what they find. Own thread + own connections.

        The summary store is only opened when there is something to write, so a
        quiet minute costs no remote round trip.
        """
        con = store = None
        try:
            con = sqlite3.connect(config.DB_PATH, timeout=30)
            found = detector.detect(con)
            prune_due = time.time() - self.last_flag_prune >= FLAG_PRUNE_EVERY
            if not found and not prune_due and not self.alert_retry:
                _note("detect", True, found=0)
                return
            store = open_store()
            if found:
                new, updated = flags.save(store, found, int(time.time()))
                log.info("flags: %d new, %d updated", new, updated)
            if config.DISCORD_WEBHOOK_URL:
                self._alert(store)
            if prune_due:
                cutoff = int(time.time() - config.FLAG_RETENTION_DAYS * 86400)
                gone = flags.prune(store, cutoff)
                alerts.prune(store, cutoff)
                self.last_flag_prune = time.time()
                if gone:
                    log.info("flag retention removed %d old flags", gone)
            _note("detect", True, found=len(found))
        except Exception as e:
            _note("detect", False, err=_err(e))
            log.exception("detector failed")
        finally:
            if con is not None:
                con.close()
            if store is not None:
                store.close()

    def _alert(self, store):
        """Send new alerts. A failure here must not stop flag saving or pruning."""
        try:
            sent, failed = alerts.send_new(store)
            self.alert_retry = failed
            if sent:
                log.info("alerts: sent %d discord message(s)", sent)
        except Exception:
            self.alert_retry = False
            log.exception("sending alerts failed")

    # ------------------------------------------------------------ outage watchdog
    def start_watchdog(self):
        """Own thread, so it still reports when the stream loop is stuck. Needs the webhook only."""
        if not (config.DISCORD_WEBHOOK_URL and config.OUTAGE_ALERTS):
            return
        self.watchdog_thread = threading.Thread(target=self._watchdog, name="watchdog", daemon=True)
        self.watchdog_thread.start()

    def _watchdog(self):
        if config.START_NOTICE:
            alerts.send_notice(
                "wikeds started",
                f"Environment: {config.ENV_NAME}. Raw events start empty after a restart, so news bursts "
                "and rate spikes need about 30 minutes before they can be flagged.", ok=True)
        while not self.stop:
            try:
                self.watchdog_tick()
            except Exception:
                log.exception("watchdog failed")
            time.sleep(WATCHDOG_EVERY)

    def check_problems(self, now=None):
        """What is wrong right now, as {name: text}. Empty = healthy."""
        now = time.time() if now is None else now
        out = {}
        quiet = now - self.last_event_at
        if quiet > config.STALL_SECONDS:
            out["ingest stalled"] = f"No events have arrived from Wikimedia for {_dur(quiet)}."
        r = STATUS["rollup"]
        since = now - (r.get("ok_at") or self.started_at)
        if since > config.ROLLUP_STALE_SECONDS:
            out["rollup"] = (f"The hourly rollup has not succeeded for {_dur(since)}"
                             + (". See the Health tab for the error." if r and not r["ok"] else "."))
        if config.DETECT_ENABLED:
            d = STATUS["detect"]
            limit = max(300, 5 * config.DETECT_EVERY_SECONDS)
            since = now - (d.get("ok_at") or self.started_at)
            if since > limit:
                out["anomaly detector"] = (f"The anomaly detector has not succeeded for {_dur(since)}"
                                           + (". See the Health tab for the error." if d and not d["ok"] else "."))
        return out

    def watchdog_tick(self, now=None, post=None):
        """Post once when a problem starts, again every OUTAGE_REMIND_SECONDS while it lasts,
        and once when it clears. A message that fails to send is retried on the next tick."""
        now = time.time() if now is None else now
        current = self.check_problems(now)
        for name, text in current.items():
            since, last_sent = self.problems.get(name, (None, 0))
            if since is None or now - last_sent >= config.OUTAGE_REMIND_SECONDS:
                if alerts.send_notice(f"Problem: {name}", text, ok=False, post=post):
                    self.problems[name] = (since or now, now)
        for name in [n for n in self.problems if n not in current]:
            since, _ = self.problems[name]
            if alerts.send_notice(f"Recovered: {name}", f"Back to normal after about {_dur(now - since)}.",
                                  ok=True, post=post):
                del self.problems[name]

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
        self.last_event_at = time.time()
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
                self.maybe_detect()
                self.maybe_log_stats()

    def run(self, install_signals=True):
        # signal handlers only work on the main thread (not when embedded)
        if install_signals:
            signal.signal(signal.SIGINT, self.request_stop)
            signal.signal(signal.SIGTERM, self.request_stop)

        self.start_watchdog()
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