"""All settings live here.

Env vars (or a .env file) always win. If a var isn't set, the default depends on
where we're running: Streamlit Community Cloud or anywhere else ("local").
"""
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()  
except ImportError:
    pass


def _detect_cloud():
    """Guess if we're on Streamlit Community Cloud. Force it with RUN_ENV=cloud|local."""
    forced = os.getenv("RUN_ENV", "").strip().lower()
    if forced in ("cloud", "local"):
        return forced == "cloud"
    # cloud mounts the repo under /mount/src and sets this hostname
    return (
        str(Path(__file__).resolve()).startswith("/mount/src/")
        or Path("/mount/src").is_dir()
        or os.getenv("HOSTNAME") == "streamlit"
    )


IS_CLOUD = _detect_cloud()
ENV_NAME = "cloud" if IS_CLOUD else "local"


def _pick(name, cloud, local):
    """Env var if set, else the default for this environment."""
    return os.getenv(name) or (cloud if IS_CLOUD else local)


def _int(name, cloud, local):
    try:
        return int(_pick(name, cloud, local))
    except (TypeError, ValueError):
        return cloud if IS_CLOUD else local


def _bool(name, cloud, local):
    return str(_pick(name, cloud, local)).strip().lower() in ("1", "true", "yes", "on")


# ---- storage ----
# raw events: sqlite on disk (on cloud this is wiped on restart, that's fine)
DB_PATH = _pick("WIKI_DB", "/tmp/wikeds/raw.db", "data/wiki.db")
# hourly summaries + meta: Supabase postgres when this is set, else the sqlite file above
SUMMARY_DB_URL = os.getenv("SUPABASE_DB_URL", "").strip()

# ---- listener ----
STREAM_URL = os.getenv("WIKI_STREAM_URL", "https://stream.wikimedia.org/v2/stream/recentchange")
# wikimedia asks for a contact in the UA, set yours in .env / secrets
USER_AGENT = os.getenv("WIKI_USER_AGENT", "wiki-edit-monitor/0.1 (your-email@example.com)")
BATCH_SIZE = _int("BATCH_SIZE", 200, 200)
FLUSH_SECONDS = _int("FLUSH_SECONDS", 5, 5)
RETENTION_DAYS = _int("RETENTION_DAYS", 1, 7)   # raw rows, small disk on cloud
CLEANUP_EVERY_SECONDS = _int("CLEANUP_EVERY_SECONDS", 600, 600)
STATS_EVERY_SECONDS = _int("STATS_EVERY_SECONDS", 60, 60)
CONNECT_TIMEOUT = _int("CONNECT_TIMEOUT", 10, 10)
READ_TIMEOUT = _int("READ_TIMEOUT", 60, 60)
MAX_BACKOFF = _int("MAX_BACKOFF", 60, 60)

# ---- anomaly flags ----
# the listener runs the detectors on its own and saves what they find
DETECT_ENABLED = _bool("DETECT_ENABLED", True, True)
DETECT_EVERY_SECONDS = _int("DETECT_EVERY_SECONDS", 60, 60)
FLAG_RETENTION_DAYS = _int("FLAG_RETENTION_DAYS", 90, 90)
EPISODE_GAP_SECONDS = _int("EPISODE_GAP_SECONDS", 1800, 1800)   # quiet this long = next flag is a new episode

# ---- alerts ----
# empty = no alerts. Keep it in secrets / .env, never in git.
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
ALERTS_PER_HOUR = _int("ALERTS_PER_HOUR", 10, 10)   # 0 = no limit

# ---- dashboard ----
TIMEZONE = os.getenv("WIKI_TZ", "Asia/Kolkata")
STALL_SECONDS = _int("STALL_SECONDS", 120, 120)
# cloud can't run a second process, so the dashboard runs the listener itself
EMBED_LISTENER = _bool("EMBED_LISTENER", True, False)

# ---- logging ----
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FILE = os.getenv("LOG_FILE", "")  # empty = console only


def setup_logging():
    """Call once at startup. Safe to call twice."""
    root = logging.getLogger()
    if getattr(root, "_wikeds_ready", False):
        return
    handlers = [logging.StreamHandler()]
    if LOG_FILE:
        Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=3))
    logging.basicConfig(
        level=LOG_LEVEL,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )
    root._wikeds_ready = True
    logging.getLogger("wiki-config").info(
        "environment=%s raw_db=%s summaries=%s embed_listener=%s",
        ENV_NAME, DB_PATH, "supabase" if SUMMARY_DB_URL else "local sqlite", EMBED_LISTENER,
    )
    logging.getLogger("wiki-config").info(
        "discord alerts: %s", "on" if DISCORD_WEBHOOK_URL else "off (no DISCORD_WEBHOOK_URL)")
    if IS_CLOUD and not SUMMARY_DB_URL:
        logging.getLogger("wiki-config").warning(
            "on cloud without SUPABASE_DB_URL: summaries will be lost on restart")