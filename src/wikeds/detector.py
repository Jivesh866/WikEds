"""Runs the anomaly detectors on the raw events and turns hits into flag dicts.

The listener calls detect() every DETECT_EVERY_SECONDS from its own thread and
hands the result to flags.save(). Thresholds are anomaly.py's defaults.
"""
import logging
import time

import pandas as pd

from wikeds import anomaly, config

log = logging.getLogger("wiki-detector")

WINDOW_MIN = 180    # 2h baseline + 15 min burst window, with some slack

_SQL = ('SELECT event_time, wiki, server_name, type, title, "user", bot, old_len, new_len, namespace, '
        "rev_new, rev_old, comment FROM edits WHERE event_time >= ?")


def read_window(con, now, minutes=WINDOW_MIN):
    """Recent raw events from the sqlite connection, same columns anomaly.py expects."""
    return pd.read_sql_query(_SQL, con, params=(int(now) - minutes * 60,))


def _f(x):
    return None if pd.isna(x) else float(x)


def _int(x):
    return None if x is None or pd.isna(x) else int(x)


def _reverted_rows(df, sus):
    """Row numbers of `sus` whose page was reverted afterwards by somebody else.
    A later revert on the same page is a strong sign the edit really was vandalism."""
    if sus.empty or "comment" not in df:
        return set()
    rv = df[(df["type"] == "edit") & anomaly.is_revert(df["comment"])][["wiki", "title", "user", "event_time"]]
    if rv.empty:
        return set()
    m = sus.reset_index().merge(rv, on=["wiki", "title"], suffixes=("", "_rv"))
    m = m[(m["event_time"] > m["edit_time"]) & (m["user_rv"] != m["user"])]
    return set(m["index"])


def collect(df, now):
    """All three detectors -> list of flag dicts with plain python types."""
    if df.empty:
        return []
    servers = df.drop_duplicates("wiki").set_index("wiki")["server_name"].to_dict()
    out = []

    def add(kind, wiki, subject, title, editor, reason, z, edits, editors, nbytes,
            rev_new=None, rev_old=None, comment=None, reverted=0):
        out.append({
            "kind": kind, "wiki": str(wiki), "subject": str(subject), "title": str(title),
            "editor": str(editor), "server": str(servers.get(wiki) or ""), "reason": reason,
            "z": _f(z), "edits": int(edits), "editors": int(editors), "bytes_changed": int(nbytes),
            "rev_new": _int(rev_new), "rev_old": _int(rev_old),
            "comment": None if comment is None or pd.isna(comment) else str(comment)[:300],
            "reverted": int(reverted),
        })

    # subject is what makes two flags "the same thing" across runs:
    # page for a burst, wiki for a spike, the single edit for vandalism
    for r in anomaly.page_bursts(df, now).to_dict("records"):
        add("burst", r["wiki"], r["title"], r["title"], "",
            f"{r['edits']} edits by {r['editors']} editors, about {r['expected']} expected",
            r["z"], r["edits"], r["editors"], r["net_bytes"])
    sus = anomaly.suspicious_edits(df, now, articles_only=config.VANDALISM_ARTICLES_ONLY,
                                   skip_wikis=config.VANDALISM_SKIP_WIKIS, skip_reverts=True,
                                   keep=("rev_new", "rev_old", "comment"))
    reverted = _reverted_rows(df, sus)
    for i, r in enumerate(sus.to_dict("records")):
        add("vandalism", r["wiki"], f"{r['title']}|{r['user']}|{r['edit_time']}", r["title"],
            r["user"], r["reason"], r["z"], 1, 1, r["bytes_changed"],
            r.get("rev_new"), r.get("rev_old"), r.get("comment"), i in reverted)
    for r in anomaly.rate_spikes(df, now).to_dict("records"):
        add("spike", r["wiki"], "", "", "",
            f"{r['recent_per_min']}/min now vs {r['baseline_per_min']}/min normally",
            r["z"], r["edits_recent"], 0, 0)
    return out


def detect(con, now=None):
    now = int(time.time() if now is None else now)
    return collect(read_window(con, now), now)