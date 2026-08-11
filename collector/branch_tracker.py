"""Track when commits from upstream main reach other branches and tags.

For fast-forwarded branches (stable, rhoai), using the commit's own date
gives 0h lead time since it's the same commit.  Instead we find the earliest
tag reachable from each branch that contains the commit, and use that tag's
tagger/commit date as the "arrived at" timestamp.  This measures how long it
took for the branch to advance past a given commit.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

from store.db import Store

log = logging.getLogger(__name__)


def _git(repo_path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", f"--git-dir={repo_path}", *args],
        capture_output=True, text=True, timeout=120,
    )
    return result.stdout.strip()


def _ref_exists(repo_path: Path, ref: str) -> bool:
    result = subprocess.run(
        ["git", f"--git-dir={repo_path}", "rev-parse", "--verify", ref],
        capture_output=True, text=True,
    )
    return result.returncode == 0


def _resolve_ref(repo_path: Path, branch: str) -> str | None:
    for candidate in [branch, f"origin/{branch}"]:
        if _ref_exists(repo_path, candidate):
            return candidate
    return None


def _commit_in_ref(repo_path: Path, sha: str, ref: str) -> bool:
    result = subprocess.run(
        ["git", f"--git-dir={repo_path}", "merge-base", "--is-ancestor", sha, ref],
        capture_output=True, text=True,
    )
    return result.returncode == 0


def _tags_containing(repo_path: Path, sha: str, pattern: str | None = None) -> list[tuple[str, str]]:
    """Return list of (tag_name, tag_date) for tags containing sha, oldest first."""
    args = ["tag", "--contains", sha, "--sort=creatordate",
            "--format=%(refname:short) %(creatordate:iso-strict)"]
    if pattern:
        args.extend(["-l", pattern])
    output = _git(repo_path, *args)
    if not output:
        return []
    results = []
    for line in output.split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split(" ", 1)
        if len(parts) == 2:
            results.append((parts[0], parts[1]))
    return results


def _earliest_tag_on_branch(
    repo_path: Path,
    sha: str,
    branch_ref: str,
    tag_pattern: str = "v*",
    branch_shas: set[str] | None = None,
) -> tuple[str | None, str | None]:
    """Find the earliest tag reachable from branch_ref that contains sha."""
    if branch_shas is not None:
        if sha not in branch_shas:
            return None, None
    elif not _commit_in_ref(repo_path, sha, branch_ref):
        return None, None

    all_tags = _tags_containing(repo_path, sha, tag_pattern)
    for tag_name, tag_date in all_tags:
        tag_sha = _git(repo_path, "rev-parse", tag_name)
        if branch_shas is not None:
            if tag_sha in branch_shas:
                return tag_name, tag_date
        elif _commit_in_ref(repo_path, tag_name, branch_ref):
            return tag_name, tag_date

    return None, None


def _branch_commit_set(repo_path: Path, ref: str) -> set[str]:
    """Return the set of all commit SHAs reachable from ref (single git call)."""
    output = _git(repo_path, "log", "--format=%H", ref)
    if not output:
        return set()
    return set(output.split("\n"))


def track_pr_propagation(
    store: Store,
    upstream_path: Path,
    cfg: dict,
    limit: int = 200,
) -> int:
    """For recent upstream PRs, track when their merge commits reached stable, rhoai, and a release tag."""
    repo_name = f"{cfg['upstream']['owner']}/{cfg['upstream']['repo']}"
    prs = store.get_merged_prs(repo=repo_name, base_branch="main")
    if not prs:
        return 0

    recent_prs = prs[-limit:]
    count = 0

    branch_refs: dict[str, str] = {}
    for branch in ["stable", "rhoai"]:
        ref = _resolve_ref(upstream_path, branch)
        if ref:
            branch_refs[branch] = ref

    # Pre-compute all commits on each branch in one git call per branch,
    # replacing N per-PR `merge-base --is-ancestor` subprocess calls with
    # O(1) set lookups.
    branch_commits: dict[str, set[str]] = {}
    for branch, ref in branch_refs.items():
        log.info("Pre-loading commit set for %s...", branch)
        branch_commits[branch] = _branch_commit_set(upstream_path, ref)
        log.info("  %s: %d commits", branch, len(branch_commits[branch]))

    expected_branches = set(branch_refs.keys())

    fully_cached = 0
    not_on_branch = 0
    for i, pr in enumerate(recent_prs):
        existing_arrivals = store.get_branch_arrivals(repo_name, pr["number"])
        existing_keys = {a["branch"] for a in existing_arrivals}
        has_all_branches = expected_branches <= existing_keys
        has_tag = any(k.startswith("tag:") for k in existing_keys)
        if has_all_branches and has_tag:
            fully_cached += 1
            continue

        merge_sha = pr.get("merge_sha") or _find_merge_sha(upstream_path, pr)
        if not merge_sha:
            continue

        found_any = False
        for branch, ref in branch_refs.items():
            if branch in existing_keys:
                continue
            bset = branch_commits.get(branch, set())
            if merge_sha not in bset:
                continue
            tag_name, tag_date = _earliest_tag_on_branch(
                upstream_path, merge_sha, ref, branch_shas=bset,
            )
            if tag_date:
                store.upsert_branch_arrival(repo_name, pr["number"], branch, tag_date)
                count += 1
                found_any = True

        if not has_tag:
            all_tags = _tags_containing(upstream_path, merge_sha, "v*")
            if all_tags:
                first_tag, first_date = all_tags[0]
                store.upsert_branch_arrival(repo_name, pr["number"], f"tag:{first_tag}", first_date)
                count += 1
                found_any = True

        if not found_any:
            not_on_branch += 1

        if (i + 1) % 50 == 0:
            log.info("Branch tracking: %d/%d PRs (%d complete, %d not yet propagated, %d arrivals stored)",
                     i + 1, len(recent_prs), fully_cached, not_on_branch, count)

    log.info("Tracked %d branch arrivals for %d PRs (%d already complete, %d not yet propagated)",
             count, len(recent_prs), fully_cached, not_on_branch)
    return count


def _find_merge_sha(repo_path: Path, pr: dict) -> str | None:
    """Find the merge commit SHA on main that corresponds to a PR."""
    merged_at = pr.get("merged_at")
    if not merged_at:
        return None
    pr_num = pr["number"]

    main_ref = _resolve_ref(repo_path, "main")
    if not main_ref:
        return None

    # Search in a wider window around the merge date
    date_prefix = merged_at[:10]
    output = _git(
        repo_path, "log", main_ref,
        f"--since={date_prefix}", f"--until={date_prefix}T23:59:59Z",
        "--format=%H %s", "--all",
    )
    for line in output.split("\n"):
        if not line.strip():
            continue
        sha, *msg_parts = line.split(" ", 1)
        msg = msg_parts[0] if msg_parts else ""
        if f"#{pr_num}" in msg or f"#{pr_num})" in msg:
            return sha

    # Broader search: try a 3-day window
    output = _git(
        repo_path, "log", main_ref,
        f"--since={date_prefix}", "--format=%H %s",
        "-50",
    )
    for line in output.split("\n"):
        if not line.strip():
            continue
        sha, *msg_parts = line.split(" ", 1)
        msg = msg_parts[0] if msg_parts else ""
        if f"#{pr_num}" in msg or f"#{pr_num})" in msg:
            return sha

    return None
