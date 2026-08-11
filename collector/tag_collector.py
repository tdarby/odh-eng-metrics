"""Collect upstream releases/tags and downstream release branches -- git-first.

Only uses the GitHub API for the Releases endpoint (1 paginated call) to get
the prerelease flag. Everything else comes from the bare clone.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

import git

from collector import github_client
from store.db import Store

log = logging.getLogger(__name__)


def _git(repo_path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", f"--git-dir={repo_path}", *args],
        capture_output=True, text=True, timeout=60,
    )
    return result.stdout.strip()


def collect_upstream_releases(
    store: Store,
    upstream_repo: git.Repo,
    cfg: dict,
) -> int:
    """Collect releases: tags + dates from git, prerelease flag from API (1 call)."""
    tag_cfg = cfg["upstream"]["tags"]
    release_re = re.compile(tag_cfg["release_pattern"])
    ea_re = re.compile(tag_cfg["ea_pattern"])
    patch_re = re.compile(tag_cfg["patch_pattern"])

    repo_path = Path(upstream_repo.common_dir)

    # Get all tags and their dates from git (zero API calls)
    tag_output = _git(repo_path, "for-each-ref", "--sort=creatordate",
                       "--format=%(refname:short) %(creatordate:iso-strict)", "refs/tags/")
    git_tags: dict[str, str] = {}
    for line in tag_output.split("\n"):
        if not line.strip():
            continue
        parts = line.split(" ", 1)
        if len(parts) == 2:
            git_tags[parts[0]] = parts[1]

    # Optionally fetch prerelease flag from GitHub Releases API (1 paginated call).
    # This is the ONLY API call in the entire tool. If it fails (rate-limited,
    # no token), we fall back to inferring prerelease from the EA tag pattern.
    prerelease_flags: dict[str, bool] = {}
    try:
        owner = cfg["upstream"]["owner"]
        repo = cfg["upstream"]["repo"]
        releases = github_client.list_releases(owner, repo)
        for rel in releases:
            prerelease_flags[rel["tag_name"]] = rel.get("prerelease", False)
        log.info("Fetched prerelease flags for %d releases via API", len(releases))
    except Exception as exc:
        log.warning("Could not fetch GitHub Releases API (%s); using EA tag pattern as fallback", exc)

    count = 0
    for tag, date in git_tags.items():
        if not release_re.match(tag) and not ea_re.match(tag):
            continue

        is_ea = bool(ea_re.match(tag))
        prerelease = prerelease_flags.get(tag, is_ea)

        store.upsert_release(
            tag=tag,
            published=date,
            prerelease=prerelease,
            is_patch=bool(patch_re.match(tag)),
            is_ea=is_ea,
        )
        count += 1

    log.info("Stored %d upstream releases from git tags", count)
    return count


def collect_downstream_branches(
    store: Store,
    downstream_repo: git.Repo,
    cfg: dict,
) -> int:
    """Enumerate rhoai-x.y branches from the local bare clone."""
    branch_cfg = cfg["downstream"]["branches"]
    release_re = re.compile(branch_cfg["release_pattern"])
    ea_re = re.compile(branch_cfg.get("ea_pattern", "$^"))

    repo_path = Path(downstream_repo.common_dir)

    # Get branches and their tip commit dates from git
    branch_output = _git(repo_path, "for-each-ref", "--sort=creatordate",
                          "--format=%(refname:short) %(creatordate:iso-strict)",
                          "refs/heads/", "refs/remotes/origin/")

    seen: set[str] = set()
    count = 0

    for line in branch_output.split("\n"):
        if not line.strip():
            continue
        parts = line.split(" ", 1)
        if len(parts) < 2:
            continue
        name, date = parts[0], parts[1]

        short = name.removeprefix("origin/")
        if short in seen:
            continue

        if not release_re.match(short) and not ea_re.match(short):
            continue

        seen.add(short)
        store.upsert_downstream_branch(
            name=short,
            first_commit_date=date,
            is_ea=bool(ea_re.match(short)),
        )
        count += 1

    log.info("Stored %d downstream release branches", count)
    return count
