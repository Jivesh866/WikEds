"""Catches a module using a setting that config.py doesn't define (stale config after a deploy).

Scans every file in the package for config.SOMETHING and checks it exists.
"""
import ast
from pathlib import Path

import wikeds
from wikeds import config

PKG = Path(wikeds.__file__).resolve().parent


def _config_names(source):
    return {n.attr for n in ast.walk(ast.parse(source))
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "config"}


def test_every_config_setting_that_is_used_exists():
    missing = {}
    for path in PKG.glob("*.py"):
        names = {n for n in _config_names(path.read_text(encoding="utf-8")) if not hasattr(config, n)}
        if names:
            missing[path.name] = sorted(names)
    assert not missing, f"settings used but not defined in config.py: {missing}"