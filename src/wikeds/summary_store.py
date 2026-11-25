"""Hourly summary storage.

Supabase (postgres) when SUPABASE_DB_URL is set, otherwise the local sqlite file.
Same tables and same upsert on both, so the rest of the code doesn't care which.
Raw events never come through here, they stay in local sqlite.
"""
import logging
import sqlite3
import threading
from pathlib import Path

import pandas as pd

from wikeds import config

log = logging.getLogger("wiki-store")

_schema_lock = threading.Lock()
_schema_ready = set()   # stores whose schema this process already checked
_PG_LOCK_KEY = 727001    # advisory lock id, any constant works

COLS = ["hour_start", "wiki", "type", "bot", "edits", "editors", "net_bytes"]

_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS summary_hourly (
        hour_start BIGINT NOT NULL,
        wiki       TEXT   NOT NULL,
        type       TEXT   NOT NULL,
        bot        INTEGER NOT NULL,
        edits      BIGINT NOT NULL,
        editors    BIGINT NOT NULL,
        net_bytes  BIGINT NOT NULL,
        PRIMARY KEY (hour_start, wiki, type, bot))""",
    """CREATE TABLE IF NOT EXISTS summary_meta (
        key   TEXT PRIMARY KEY,
        value BIGINT NOT NULL)""",
]
# supabase exposes public tables over its REST api, so lock them down.
# our direct postgres connection bypasses RLS, so nothing changes for us.
_PG_EXTRA = [
    "ALTER TABLE summary_hourly ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE summary_meta ENABLE ROW LEVEL SECURITY",
]

# only overwrite an hour if the new count is not smaller: after a cloud restart the
# raw table is empty, so a recompute of the current hour would otherwise shrink it
_UPSERT = (
    "INSERT INTO summary_hourly (" + ", ".join(COLS) + ") VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p}) "
    "ON CONFLICT (hour_start, wiki, type, bot) DO UPDATE SET "
    "edits = excluded.edits, editors = excluded.editors, net_bytes = excluded.net_bytes "
    "WHERE excluded.edits >= summary_hourly.edits"
)
_SET_META = ("INSERT INTO summary_meta (key, value) VALUES ({p}, {p}) "
             "ON CONFLICT (key) DO UPDATE SET value = excluded.value")
_GET_META = "SELECT value FROM summary_meta WHERE key = {p}"
_PRUNE = "DELETE FROM summary_hourly WHERE hour_start < {p}"
_READ = "SELECT " + ", ".join(COLS) + " FROM summary_hourly WHERE hour_start >= {p}"


class Store:
    def __init__(self, con, placeholder, label):
        self.con = con
        self.p = placeholder
        self.label = label

    def _tx(self):
        # sqlite connection is its own transaction context, psycopg needs .transaction()
        return self.con if self.p == "?" else self.con.transaction()

    def _exec(self, sql, params=()):
        cur = self.con.cursor()
        cur.execute(sql.format(p=self.p), params)
        return cur

    def _pg_tables_exist(self):
        row = self._exec(
            "SELECT to_regclass('public.summary_hourly') IS NOT NULL "
            "AND to_regclass('public.summary_meta') IS NOT NULL"
        ).fetchone()
        return bool(row[0])

    def ensure_schema(self):
        """Create tables once. Postgres can't create the same table from two
        connections at the same moment (listener + dashboard on startup), so check
        first and serialize the create with an advisory lock."""
        with _schema_lock:
            if self.label in _schema_ready:
                return
            is_pg = self.p != "?"
            if is_pg and self._pg_tables_exist():
                _schema_ready.add(self.label)
                return
            with self._tx():
                cur = self.con.cursor()
                if is_pg:
                    cur.execute("SELECT pg_advisory_xact_lock(%s)", (_PG_LOCK_KEY,))
                for stmt in _SCHEMA:
                    cur.execute(stmt)
                if is_pg:
                    for stmt in _PG_EXTRA:
                        cur.execute(stmt)
            _schema_ready.add(self.label)

    def get_meta(self, key):
        row = self._exec(_GET_META, (key,)).fetchone()
        return None if row is None else int(row[0])

    def set_meta(self, key, value):
        with self._tx():
            self._exec(_SET_META, (key, int(value)))

    def upsert_hours(self, rows):
        if not rows:
            return
        with self._tx():
            cur = self.con.cursor()
            cur.executemany(_UPSERT.format(p=self.p), rows)

    def prune(self, cutoff):
        with self._tx():
            return self._exec(_PRUNE, (int(cutoff),)).rowcount

    def read_hours(self, since_hour):
        rows = self._exec(_READ, (int(since_hour),)).fetchall()
        return pd.DataFrame(rows, columns=COLS)

    def close(self):
        try:
            self.con.close()
        except Exception:
            pass


def open_store():
    """New connection to wherever summaries live. Caller closes it."""
    if config.SUMMARY_DB_URL:
        import psycopg  # only needed when supabase is configured

        con = psycopg.connect(
            config.SUMMARY_DB_URL,
            autocommit=True,
            prepare_threshold=None,   # supabase's transaction pooler can't do prepared statements
            connect_timeout=10,
        )
        store = Store(con, "%s", "Supabase")
    else:
        Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(config.DB_PATH, timeout=30)
        con.execute("PRAGMA journal_mode=WAL;")
        store = Store(con, "?", "local sqlite")
    try:
        store.ensure_schema()
    except Exception:
        store.close()
        raise
    return store