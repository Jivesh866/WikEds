"""Discord alerts for new flags.

Only news bursts and blanked pages alert. Each flag episode sends one message:
what was already sent is remembered in the alert_log table (same database as the
flags), so restarts and repeat detections stay quiet. At most ALERTS_PER_HOUR
messages go out per hour, the rest are folded into one "N more" message.

The webhook url is a secret: it is never logged. Without DISCORD_WEBHOOK_URL
nothing happens.

Test your webhook:  uv run python -m wikeds.alerts
"""
import logging
import threading
import time
from datetime import datetime, timezone
from urllib.parse import quote

import requests

from wikeds import config, flags

log = logging.getLogger("wiki-alerts")

LOOKBACK = 3600           # flags older than this are too stale to alert on
BLANKED = "page blanked"  # reason text from anomaly.suspicious_edits
COLORS = {"burst": 0xEB6834, "vandalism": 0xD03030}

_lock = threading.Lock()
_ready = set()
_PG_LOCK_KEY = 727003     # advisory lock id, different from the other tables

_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS alert_log (
        flag_id   TEXT PRIMARY KEY,
        sent_at   BIGINT NOT NULL,
        delivered INTEGER NOT NULL)""",     # 0 = folded into a "N more" message
    "CREATE INDEX IF NOT EXISTS idx_alert_sent ON alert_log (sent_at)",
]

_PENDING = (
    "SELECT f.flag_id, f.kind, f.wiki, f.title, f.editor, f.server, f.reason, f.peak_z, "
    "f.edits, f.editors, f.bytes_changed, f.first_seen FROM anomaly_flags f "
    "WHERE f.first_seen >= {p} AND (f.kind = 'burst' OR (f.kind = 'vandalism' AND f.reason = {p})) "
    "AND NOT EXISTS (SELECT 1 FROM alert_log a WHERE a.flag_id = f.flag_id) "
    "ORDER BY f.first_seen")
_COLS = ["flag_id", "kind", "wiki", "title", "editor", "server", "reason", "peak_z",
         "edits", "editors", "bytes_changed", "first_seen"]
_MARK = ("INSERT INTO alert_log (flag_id, sent_at, delivered) VALUES ({p}, {p}, {p}) "
         "ON CONFLICT (flag_id) DO NOTHING")
_RECENT = "SELECT COUNT(*) FROM alert_log WHERE sent_at >= {p} AND delivered = 1"


def ensure_schema(store):
    is_pg = store.p != "?"
    with _lock:
        if is_pg and store.label in _ready:
            return
        if is_pg and store._exec("SELECT to_regclass('public.alert_log') IS NOT NULL").fetchone()[0]:
            _ready.add(store.label)
            return
        with store._tx():
            cur = store.con.cursor()
            if is_pg:
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (_PG_LOCK_KEY,))
            for stmt in _SCHEMA:
                cur.execute(stmt)
            if is_pg:
                cur.execute("ALTER TABLE alert_log ENABLE ROW LEVEL SECURITY")
        _ready.add(store.label)


def prune(store, cutoff):
    ensure_schema(store)
    with store._tx():
        return store._exec("DELETE FROM alert_log WHERE sent_at < {p}", (int(cutoff),)).rowcount


# ---------------------------------------------------------------- message text
def _clip(s, n):
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def _md(s):
    """Wikipedia titles and usernames are user text, so keep them from turning into markdown."""
    return "".join("\\" + c if c in "\\*_~`|>[]()#:@" else c for c in str(s))


def _page_url(server, title, history=False):
    if not server or not title:
        return None
    url = f"https://{server}/wiki/{quote(str(title).replace(' ', '_'), safe='')}"
    return url + "?action=history" if history else url


def build_message(row):
    """Discord webhook body for one flag. Mentions are switched off so a page title
    like @everyone can't ping anyone."""
    page, hist = _page_url(row["server"], row["title"]), _page_url(row["server"], row["title"], True)
    links = f"[Open page]({page}) · [History]({hist})" if page else ""
    if row["kind"] == "burst":
        title = f"News burst: {row['title']}"
        z = "" if row["peak_z"] is None else f", z {row['peak_z']:.1f}"
        text = f"{row['edits']} edits by {row['editors']} editors in about 15 minutes{z}"
    else:
        title = f"Page blanked: {row['title']}"
        text = f"{_md(row['editor'])} removed {abs(int(row['bytes_changed'])):,} bytes"
    embed = {
        "title": _clip(title, 250),
        "description": _clip(f"{text}\n{links}".strip(), 2000),
        "color": COLORS.get(row["kind"], 0x888888),
        "footer": {"text": row["wiki"]},
        "timestamp": datetime.fromtimestamp(int(row["first_seen"]), tz=timezone.utc).isoformat(),
    }
    if page:
        embed["url"] = page
    return {"username": "wikeds", "embeds": [embed], "allowed_mentions": {"parse": []}}


def _post(url, body, post):
    """True if Discord accepted it. Logs the status only, never the url."""
    try:
        r = post(url, json=body, timeout=10)
    except requests.RequestException as e:
        log.warning("discord post failed: %s", type(e).__name__)
        return False
    if r.status_code in (200, 204):
        return True
    log.warning("discord returned %s, will retry", r.status_code)   # 429 = we're being rate limited
    return False


# ---------------------------------------------------------------- sending
def send_new(store, now=None, url=None, limit=None, post=None):
    """Send alerts for flags that haven't been alerted yet.

    Returns (messages sent, failed). On the first failure it stops and leaves the
    rest unmarked, so the next run retries them.
    """
    url = config.DISCORD_WEBHOOK_URL if url is None else url
    if not url:
        return 0, False
    post = requests.post if post is None else post
    limit = config.ALERTS_PER_HOUR if limit is None else int(limit)
    now = int(time.time() if now is None else now)

    flags.ensure_schema(store)
    ensure_schema(store)
    rows = [dict(zip(_COLS, r)) for r in
            store._exec(_PENDING, (now - LOOKBACK, BLANKED)).fetchall()]
    if not rows:
        return 0, False

    recent = store._exec(_RECENT, (now - 3600,)).fetchone()[0]
    budget = None if limit <= 0 else max(limit - int(recent), 0)

    def mark(flag_id, delivered):
        with store._tx():
            store._exec(_MARK, (flag_id, now, delivered))

    sent = 0
    for i, row in enumerate(rows):
        if budget is not None and sent >= budget:
            rest = rows[i:]
            body = {"username": "wikeds", "allowed_mentions": {"parse": []},
                    "content": f"{len(rest)} more flag(s) were not sent because of the limit of "
                               f"{limit} alerts per hour. See the Flags tab."}
            if _post(url, body, post):
                for r in rest:
                    mark(r["flag_id"], 0)
            else:
                return sent, True
            break
        if not _post(url, build_message(row), post):
            return sent, True
        mark(row["flag_id"], 1)
        sent += 1
    return sent, False


def _main():
    config.setup_logging()
    if not config.DISCORD_WEBHOOK_URL:
        print("DISCORD_WEBHOOK_URL is not set.")
        return
    body = {"username": "wikeds", "allowed_mentions": {"parse": []},
            "content": "wikeds test message: Discord alerts are connected."}
    print("sent" if _post(config.DISCORD_WEBHOOK_URL, body, requests.post) else "failed, see the log")


if __name__ == "__main__":
    _main()