"""The secrets stage checks gitleaks' scanned-commit count against `commits_with_diffs`.

gitleaks counts a commit as scanned when `git log -p` with its log options shows a diff for it. The helper must
count the same set: merges included (the stage passes -m, so content written while resolving a conflict is
scanned), empty commits excluded (no diff, so gitleaks never counts them and a strict check would fail).
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCAN_SCRIPT = ROOT / "scripts" / "security-scan.sh"


def _helper() -> str:
    script = SCAN_SCRIPT.read_text()
    opts = re.search(r"^GITLEAKS_LOG_OPTS=\(.*\)$", script, re.M)
    body = re.search(r"^commits_with_diffs\(\) \{\n.*?^\}", script, re.S | re.M)
    assert opts, "scripts/security-scan.sh defines GITLEAKS_LOG_OPTS"
    assert body, "scripts/security-scan.sh defines commits_with_diffs()"
    return f"set -euo pipefail\n{opts.group(0)}\n{body.group(0)}\ncommits_with_diffs\n"


@pytest.fixture
def git_env(tmp_path: Path) -> dict[str, str]:
    # Isolated from the developer's git configuration (signing, hooks, default branch).
    return {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }


def _git(repo: Path, env: dict[str, str], *args: str, check: bool = True) -> None:
    subprocess.run(["git", *args], cwd=repo, env=env, check=check, capture_output=True)


def _count(repo: Path, env: dict[str, str]) -> int:
    result = subprocess.run(["bash", "-c", _helper()], cwd=repo, env=env, check=True, capture_output=True, text=True)
    return int(result.stdout.strip())


def test_counts_merges_and_ignores_empty_commits(tmp_path: Path, git_env: dict[str, str]) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, git_env, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("base\n")
    _git(repo, git_env, "add", "a.txt")
    _git(repo, git_env, "commit", "-q", "-m", "base")
    _git(repo, git_env, "commit", "-q", "--allow-empty", "-m", "empty")
    # A merge whose conflict resolution writes content that neither parent has.
    _git(repo, git_env, "switch", "-q", "-c", "topic")
    (repo / "a.txt").write_text("topic\n")
    _git(repo, git_env, "commit", "-q", "-am", "topic")
    _git(repo, git_env, "switch", "-q", "main")
    (repo / "a.txt").write_text("main\n")
    _git(repo, git_env, "commit", "-q", "-am", "main")
    _git(repo, git_env, "merge", "-q", "topic", check=False)
    (repo / "a.txt").write_text("resolved\n")
    _git(repo, git_env, "commit", "-q", "-am", "resolve")
    # A clean merge.
    _git(repo, git_env, "switch", "-q", "-c", "side")
    (repo / "b.txt").write_text("side\n")
    _git(repo, git_env, "add", "b.txt")
    _git(repo, git_env, "commit", "-q", "-m", "side")
    _git(repo, git_env, "switch", "-q", "main")
    (repo / "c.txt").write_text("main\n")
    _git(repo, git_env, "add", "c.txt")
    _git(repo, git_env, "commit", "-q", "-m", "more")
    _git(repo, git_env, "merge", "-q", "--no-edit", "side")

    # 8 commits: base, empty, topic, main, resolve (merge), side, more, clean merge. All but `empty` have a diff;
    # without -m the two merges would show none and the count would be 5.
    assert _count(repo, git_env) == 7


def test_counts_commits_on_every_ref(tmp_path: Path, git_env: dict[str, str]) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, git_env, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("base\n")
    _git(repo, git_env, "add", "a.txt")
    _git(repo, git_env, "commit", "-q", "-m", "base")
    _git(repo, git_env, "switch", "-q", "-c", "unmerged")
    (repo / "u.txt").write_text("unmerged\n")
    _git(repo, git_env, "add", "u.txt")
    _git(repo, git_env, "commit", "-q", "-m", "unmerged")
    _git(repo, git_env, "switch", "-q", "main")

    assert _count(repo, git_env) == 2
