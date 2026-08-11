"""Manage local bare clones of upstream and downstream repos."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import git

log = logging.getLogger(__name__)


def _repo_dir(data_dir: Path, name: str) -> Path:
    return data_dir / "repos" / f"{name}.git"


def _ensure_fetch_refspec(repo_path: Path) -> None:
    """Ensure the bare clone has a fetch refspec so branches actually update.

    GitPython's bare clone sometimes omits the refspec, causing fetch to
    download objects without updating any local refs.
    """
    result = subprocess.run(
        ["git", f"--git-dir={repo_path}", "config", "--get-all",
         "remote.origin.fetch"],
        capture_output=True, text=True,
    )
    expected = "+refs/heads/*:refs/heads/*"
    if expected not in (result.stdout or ""):
        log.info("Fixing missing fetch refspec for %s", repo_path.name)
        subprocess.run(
            ["git", f"--git-dir={repo_path}", "config", "remote.origin.fetch",
             expected],
            check=True, capture_output=True, text=True,
        )


def ensure_repo(data_dir: Path, name: str, clone_url: str) -> git.Repo:
    """Clone (bare) on first run, fetch --prune on subsequent runs."""
    repo_path = _repo_dir(data_dir, name)
    if repo_path.exists():
        log.info("Fetching updates for %s", name)
        repo = git.Repo(str(repo_path))
        _ensure_fetch_refspec(repo_path)
        _fetch_all(repo_path)
        return repo

    log.info("Cloning %s (bare) into %s", clone_url, repo_path)
    repo_path.parent.mkdir(parents=True, exist_ok=True)
    repo = git.Repo.clone_from(clone_url, str(repo_path), bare=True)
    _ensure_fetch_refspec(repo_path)
    return repo


def _fetch_all(repo_path: Path) -> None:
    """Fetch all remotes with prune and tags."""
    subprocess.run(
        ["git", f"--git-dir={repo_path}", "fetch", "--all", "--prune", "--tags"],
        check=True, capture_output=True, text=True,
    )


def ensure_repos(cfg: dict, data_dir: Path) -> tuple[git.Repo, git.Repo]:
    """Return (upstream_repo, downstream_repo), cloning or fetching as needed."""
    upstream = ensure_repo(
        data_dir,
        cfg["upstream"]["repo"],
        cfg["upstream"]["clone_url"],
    )
    downstream = ensure_repo(
        data_dir,
        cfg["downstream"]["repo"],
        cfg["downstream"]["clone_url"],
    )
    return upstream, downstream
