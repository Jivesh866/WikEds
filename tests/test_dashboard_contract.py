"""Catches deploy mismatches like the old events_frame error.

Reads dashboard.py without running it and checks that every agg.something()
call it makes exists on Aggregator.
"""
import ast
from pathlib import Path

import pytest

pytest.importorskip("duckdb")

import wikeds  # noqa: E402
from wikeds.aggregator import Aggregator  # noqa: E402

DASHBOARD = Path(wikeds.__file__).resolve().parent / "dashboard.py"


def _agg_calls(source):
    names = set()
    for node in ast.walk(ast.parse(source)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "agg"):
            names.add(node.func.attr)
    return names


def test_dashboard_only_calls_methods_aggregator_has():
    src = DASHBOARD.read_text(encoding="utf-8")
    missing = sorted(n for n in _agg_calls(src) if not callable(getattr(Aggregator, n, None)))
    assert not missing, f"dashboard.py calls Aggregator methods that don't exist: {missing}"