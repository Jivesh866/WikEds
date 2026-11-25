import os
import sys
import tempfile
from pathlib import Path

# set before wikeds.config is imported: never touch a real database from tests.
# (empty string, not unset, so a local .env can't fill it back in)
os.environ["RUN_ENV"] = "local"
os.environ["SUPABASE_DB_URL"] = ""
os.environ["WIKI_DB"] = str(Path(tempfile.gettempdir()) / "wikeds-tests" / "raw.db")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))