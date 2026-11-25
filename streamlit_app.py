"""Streamlit Cloud entry point: point the app at this file.

Cloud vs local settings are picked automatically in config.py.
"""
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))  # so `import wikeds` works without installing

runpy.run_path(str(ROOT / "src" / "wikeds" / "dashboard.py"), run_name="__main__")