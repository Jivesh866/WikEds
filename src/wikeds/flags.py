"""Saved anomaly flags, so they can be reviewed and labeled later.

Lives in the same database as the hourly summaries (Supabase or local sqlite) and
reuses the Store connection from summary_store. One row per episode: while the
same page / wiki keeps getting flagged, its row is updated instead of a new one
being added. Once it has been quiet for EPISODE_GAP_SECONDS, the next flag starts
a new episode.
"""
import logging
import threading
import time

import pandas as pd

from wikeds import config

log = logging.getLogger("wiki-flags")

LABELS = ["real news", "vandalism", "false alarm", "unsure"]
_DECIDED = ("real news", "vandalism", "false alarm")   # "unsure" doesn't count either way
_REAL = ("real news", "vandalism")

_lock = threading.Lock()
_ready = set()
_PG_LOCK_KEY = 727002    # advisory lock id, different from summary_store's

_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS anomaly_flags (
        flag_id       TEXT PRIMARY KEY,
        kind          TEXT NOT NULL,
        wiki          TEXT NOT NULL,
        subject       TEXT NOT NULL,
        title         TEXT NOT NULL,
        editor        TEXT NOT NULL,
        server        TEXT NOT NULL,
        reason        TEXT NOT NULL,
        peak_z        DOUBLE PRECISION,
        edits         BIGINT NOT NULL,
        editors       BIGINT NOT NULL,
        bytes_changed BIGINT NOT NULL,
        first_seen    BIGINT NOT NULL,
        last_seen     BIGINT NOT NULL,
        label         TEXT,
        labeled_at    BIGINT)""",
    "CREATE INDEX IF NOT EXISTS idx_flags_last_seen ON anomaly_flags (last_seen)",
]

_INSERT_COLS = ["flag_id", "kind", "wiki", "subject", "title", "editor", "server", "reason",
                "peak_z", "edits", "editors", "bytes_changed", "first_seen", "last_seen"]
_INSERT = ("INSERT INTO anomaly_flags (" + ", ".join(_INSERT_COLS) + ") VALUES ("
           + ", ".join(["{p}"] * len(_INSERT_COLS)) + ") ON CONFLICT (flag_id) DO NOTHING")
# keep the biggest score / counts seen during the episode
_UPDATE = ("UPDATE anomaly_flags SET last_seen = {p}, "
           "peak_z = CASE WHEN {p} > peak_z THEN {p} ELSE peak_z END, "
           "edits = CASE WHEN {p} > edits THEN {p} ELSE edits END, "
           "editors = CASE WHEN {p} > editors THEN {p} ELSE editors END "
           "WHERE flag_id = {p}")
_OPEN = ("SELECT flag_id, kind, wiki, subject FROM anomaly_flags "
         "WHERE last_seen >= {p} ORDER BY last_seen")

READ_COLS = ["flag_id", "kind", "wiki", "title", "editor", "server", "reason", "peak_z",
             "edits", "editors", "bytes_changed", "first_seen", "last_seen", "label"]
_READ = ("SELECT " + ", ".join(READ_COLS) + " FROM anomaly_flags "
         "WHERE last_seen >= {p} ORDER BY last_seen DESC LIMIT {p}")


def ensure_schema(store):
    """Create the table once. Same idea as summary_store: check first, then
    serialize the create with an advisory lock on postgres."""
    is_pg = store.p != "?"
    with _lock:
        if is_pg and store.label in _ready:
            return
        if is_pg and store._exec("SELECT to_regclass('public.anomaly_flags') IS NOT NULL").fetchone()[0]:
            _ready.add(store.label)
            return
        with store._tx():
            cur = store.con.cursor()
            if is_pg:
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (_PG_LOCK_KEY,))
            for stmt in _SCHEMA:
                cur.execute(stmt)
            if is_pg:
                cur.execute("ALTER TABLE anomaly_flags ENABLE ROW LEVEL SECURITY")
        _ready.add(store.label)


def save(store, found, now, gap=None):
    """Store flags from one detector run. Returns (new episodes, updated episodes)."""
    if not found:
        return 0, 0
    ensure_schema(store)
    now = int(now)
    gap = config.EPISODE_GAP_SECONDS if gap is None else int(gap)

    # newest row wins if a key somehow appears twice
    open_ = {(k, w, s): fid for fid, k, w, s in store._exec(_OPEN, (now - gap,)).fetchall()}
    inserts, updates = [], []
    for f in found:
        fid = open_.get((f["kind"], f["wiki"], f["subject"]))
        if fid is None:
            fid = f"{f['kind']}|{f['wiki']}|{f['subject']}|{now}"
            inserts.append((fid, f["kind"], f["wiki"], f["subject"], f["title"], f["editor"],
                            f["server"], f["reason"], f["z"], f["edits"], f["editors"],
                            f["bytes_changed"], now, now))
        else:
            updates.append((now, f["z"], f["z"], f["edits"], f["edits"],
                            f["editors"], f["editors"], fid))
    with store._tx():
        cur = store.con.cursor()
        if inserts:
            cur.executemany(_INSERT.format(p=store.p), inserts)
        if updates:
            cur.executemany(_UPDATE.format(p=store.p), updates)
    return len(inserts), len(updates)


def read(store, since, limit=1000):
    """Flags last seen since `since` (unix s), newest first."""
    ensure_schema(store)
    rows = store._exec(_READ, (int(since), int(limit))).fetchall()
    df = pd.DataFrame(rows, columns=READ_COLS)
    df["peak_z"] = pd.to_numeric(df["peak_z"])
    return df


def set_label(store, flag_id, label):
    """Label one flag. None or "" clears it."""
    label = label or None
    if label is not None and label not in LABELS:
        raise ValueError(f"unknown label: {label}")
    ensure_schema(store)
    with store._tx():
        store._exec("UPDATE anomaly_flags SET label = {p}, labeled_at = {p} WHERE flag_id = {p}",
                    (label, None if label is None else int(time.time()), flag_id))


def prune(store, cutoff):
    """Delete flags last seen before `cutoff`. Returns rows deleted."""
    ensure_schema(store)
    with store._tx():
        return store._exec("DELETE FROM anomaly_flags WHERE last_seen < {p}", (int(cutoff),)).rowcount


def precision(df, kind_names=None):
    """Per detector: how many flags, how many reviewed, how many were real."""
    names = kind_names or {}
    cols = ["detector", "flags", "reviewed", "real", "false alarms", "precision"]
    rows = []
    for kind, g in df.groupby("kind"):
        decided = g[g["label"].isin(_DECIDED)]
        real = int(decided["label"].isin(_REAL).sum())
        rows.append({
            "detector": names.get(kind, kind), "flags": len(g), "reviewed": len(decided),
            "real": real, "false alarms": len(decided) - real,
            "precision": f"{100 * real / len(decided):.0f}%" if len(decided) else "n/a",
        })
    return pd.DataFrame(rows, columns=cols)