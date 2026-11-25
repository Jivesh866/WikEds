"""One command to run everything locally: listener + dashboard."""
import logging
import subprocess
import sys
import time
from pathlib import Path

from wikeds import config

log = logging.getLogger("wiki-run")
DASHBOARD = Path(__file__).with_name("dashboard.py")


def _start_listener():
    return subprocess.Popen([sys.executable, "-m", "wikeds.listener"])


def main():
    config.setup_logging()
    listener = _start_listener()
    dash = subprocess.Popen([sys.executable, "-m", "streamlit", "run", str(DASHBOARD)])
    log.info("started listener (pid %d) and dashboard (pid %d)", listener.pid, dash.pid)

    try:
        while dash.poll() is None:
            if listener.poll() is not None:
                log.error("listener exited with %s, restarting in 5s", listener.returncode)
                time.sleep(5)
                listener = _start_listener()
            time.sleep(2)
    except KeyboardInterrupt:
        pass
    finally:
        for p in (dash, listener):
            if p.poll() is None:
                p.terminate()
        for p in (dash, listener):
            try:
                p.wait(timeout=15)  # listener flushes its buffer on SIGTERM
            except subprocess.TimeoutExpired:
                p.kill()
        log.info("all stopped")


if __name__ == "__main__":
    main()