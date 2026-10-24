"""The test pyramid is selectable by layer: `pytest -m unit|integration|security|evaluation`."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _collected(marker: str, path: str) -> tuple[int, int]:
    """Return (selected, deselected) for `pytest --co -m <marker> <path>`."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--co", "-q", "-p", "no:cacheprovider", "-m", marker, path],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    summary = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    selected = re.search(r"(\d+)(?:/\d+)? tests? collected", summary)
    deselected = re.search(r"(\d+) deselected", summary)
    return (int(selected.group(1)) if selected else 0, int(deselected.group(1)) if deselected else 0)


@pytest.mark.parametrize(
    ("marker", "own_dir", "other_dir"),
    [
        ("unit", "tests/unit/domain", "tests/integration/test_security.py"),
        ("integration", "tests/integration/test_security.py", "tests/unit/domain"),
        ("security", "tests/security/test_cors.py", "tests/unit/domain"),
        ("evaluation", "tests/evaluation/test_metrics.py", "tests/unit/domain"),
    ],
)
def test_each_layer_is_selected_by_its_marker_only(marker: str, own_dir: str, other_dir: str) -> None:
    selected, _ = _collected(marker, own_dir)
    assert selected > 0, f"-m {marker} selects nothing in {own_dir}"
    selected_elsewhere, deselected_elsewhere = _collected(marker, other_dir)
    assert selected_elsewhere == 0
    assert deselected_elsewhere > 0
