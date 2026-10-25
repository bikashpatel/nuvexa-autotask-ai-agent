"""CI helper scripts run on the runner's system `python3`, not the project's Python 3.14.

ubuntu-24.04 ships Python 3.12, so the standard-library scripts in scripts/ and the inline Python in the
workflow and scan script must parse with the 3.12 grammar. Ruff formats the repository for 3.14 (for
example unparenthesized `except A, B:`), which 3.12 rejects with a SyntaxError.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
RUNNER_PYTHON = (3, 12)  # ubuntu-24.04 system python3
STDLIB_SCRIPTS = sorted((ROOT / "scripts").glob("*.py"))
_HEREDOC = re.compile(r"python3 - [^\n]*<<'PY'\n(.*?)\n\s*PY\n", re.S)


def _inline_python(path: Path) -> list[str]:
    blocks = _HEREDOC.findall(path.read_text())
    # Heredocs inside YAML block scalars are indented; ast needs them flush left.
    return [_dedent(block) for block in blocks]


def _dedent(block: str) -> str:
    lines = block.splitlines()
    indent = min((len(line) - len(line.lstrip()) for line in lines if line.strip()), default=0)
    return "\n".join(line[indent:] for line in lines)


@pytest.mark.parametrize("path", STDLIB_SCRIPTS, ids=lambda p: p.name)
def test_stdlib_scripts_parse_on_the_runner_python(path: Path) -> None:
    ast.parse(path.read_text(), filename=str(path), feature_version=RUNNER_PYTHON)


@pytest.mark.parametrize(
    "path", [ROOT / ".github" / "workflows" / "ci.yml", ROOT / "scripts" / "security-scan.sh"], ids=lambda p: p.name
)
def test_inline_python_parses_on_the_runner_python(path: Path) -> None:
    blocks = _inline_python(path)
    assert blocks, f"expected inline python in {path.name}"
    for block in blocks:
        ast.parse(block, filename=str(path), feature_version=RUNNER_PYTHON)


def test_the_check_detects_314_only_syntax() -> None:
    with pytest.raises(SyntaxError):
        ast.parse("try:\n    pass\nexcept OSError, ValueError:\n    pass\n", feature_version=RUNNER_PYTHON)
