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


class _FakeItem:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.markers: list[pytest.MarkDecorator] = []

    def add_marker(self, marker: pytest.MarkDecorator) -> None:
        self.markers.append(marker)


def test_a_test_outside_every_layer_directory_fails_collection() -> None:
    # CI runs the layers as separate steps (-m unit, -m integration, ...); a test that belongs to no
    # layer would silently run in none of them.
    from tests import conftest

    root = Path(conftest.__file__).resolve().parent
    layered = _FakeItem(root / "unit" / "domain" / "test_state_machine.py")
    stray = _FakeItem(root / "test_stray.py")
    unknown_dir = _FakeItem(root / "support" / "test_helpers.py")
    with pytest.raises(pytest.UsageError, match=r"test_stray\.py") as excinfo:
        conftest.pytest_collection_modifyitems(config=None, items=[layered, stray, unknown_dir])  # type: ignore[arg-type]
    assert "support/test_helpers.py" in str(excinfo.value)
    assert "test_state_machine.py" not in str(excinfo.value)


def test_items_outside_the_tests_tree_are_left_alone() -> None:
    from tests import conftest

    root = Path(conftest.__file__).resolve().parent
    e2e_item = _FakeItem(root.parent / "e2e" / "test_api_system.py")
    conftest.pytest_collection_modifyitems(config=None, items=[e2e_item])  # type: ignore[arg-type]
    assert e2e_item.markers == []


def test_the_four_layers_together_select_the_whole_suite() -> None:
    selected, _ = _collected("not (unit or integration or security or evaluation)", "tests")
    assert selected == 0
