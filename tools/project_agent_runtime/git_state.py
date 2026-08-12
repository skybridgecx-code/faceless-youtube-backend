from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


class GitInspectionError(RuntimeError):
    """Raised when deterministic repository inspection cannot complete."""


@dataclass(frozen=True)
class RepoState:
    root: Path
    branch: str
    head: str
    origin_url: str
    normalized_origin: str
    status_lines: tuple[str, ...]
    staged_files: tuple[str, ...]

    @property
    def clean(self) -> bool:
        return not self.status_lines

    @property
    def detached(self) -> bool:
        return not self.branch


def _git(repo_root: Path, *args: str) -> str:
    env = dict(os.environ)
    env.update(
        {
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "cat",
            "LC_ALL": "C",
        }
    )
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitInspectionError(f"git inspection failed: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise GitInspectionError(
            f"git {' '.join(args)} failed with exit {completed.returncode}: {detail}"
        )
    return completed.stdout.rstrip("\n")


def normalize_github_remote(url: str) -> str:
    value = url.strip().removesuffix("/")
    if value.startswith("git@github.com:"):
        value = value[len("git@github.com:") :]
    elif value.startswith("ssh://git@github.com/"):
        value = value[len("ssh://git@github.com/") :]
    elif value.startswith("https://github.com/"):
        value = value[len("https://github.com/") :]
    elif value.startswith("http://github.com/"):
        value = value[len("http://github.com/") :]
    else:
        parsed = urlparse(value)
        if parsed.hostname == "github.com":
            value = parsed.path.lstrip("/")
    return value.removesuffix(".git")


def inspect_repo(repo_root: Path) -> RepoState:
    requested = repo_root.resolve()
    actual_root = Path(_git(requested, "rev-parse", "--show-toplevel")).resolve()
    branch = _git(actual_root, "branch", "--show-current").strip()
    head = _git(actual_root, "rev-parse", "HEAD").strip()
    origin = _git(actual_root, "remote", "get-url", "origin").strip()
    status = tuple(
        line for line in _git(actual_root, "status", "--porcelain=v1").splitlines() if line
    )
    staged = tuple(
        line
        for line in _git(actual_root, "diff", "--cached", "--name-only").splitlines()
        if line
    )
    return RepoState(
        root=actual_root,
        branch=branch,
        head=head,
        origin_url=origin,
        normalized_origin=normalize_github_remote(origin),
        status_lines=status,
        staged_files=staged,
    )
