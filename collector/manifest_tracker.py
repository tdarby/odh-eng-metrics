"""Track component manifest SHA pins and upstream changelogs.

Parses get_all_manifests.sh from the operator git history to extract which
upstream component SHAs are pinned at each manifest-update PR.  Optionally
fetches the GitHub compare API to get the commit delta between consecutive
SHA bumps for each component.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import httpx

from store.db import Store

log = logging.getLogger(__name__)

# Matches bash associative array entries like:
#   ["kserve"]="opendatahub-io:kserve:release-v0.17@d843557:config"
_ENTRY_RE = re.compile(
    r'\["(?P<key>[^"]+)"\]="(?P<org>[^:]+):(?P<repo>[^:]+):(?P<ref>[^:]+):(?P<path>[^"]+)"'
)

# We only care about ODH manifests + charts (upstream); RHOAI uses forks
_ARRAY_NAMES = {"ODH_COMPONENT_MANIFESTS", "ODH_COMPONENT_CHARTS"}


def _parse_manifest_entries(script_text: str) -> list[dict]:
    """Extract component manifest pins from get_all_manifests.sh content."""
    results = []
    current_array: str | None = None

    for line in script_text.splitlines():
        stripped = line.strip()

        # Detect array start
        for name in _ARRAY_NAMES:
            if stripped.startswith(f"declare -A {name}=("):
                current_array = name
                break

        if current_array is None:
            continue

        # Detect array end
        if stripped == ")":
            current_array = None
            continue

        m = _ENTRY_RE.search(stripped)
        if not m:
            continue

        ref = m.group("ref")
        branch = None
        pinned_sha = None

        # branch@sha format
        if "@" in ref:
            branch, pinned_sha = ref.split("@", 1)
        elif re.fullmatch(r"[a-f0-9]{7,40}", ref):
            pinned_sha = ref
        else:
            branch = ref

        org = m.group("org")
        repo = m.group("repo")

        results.append({
            "component": m.group("key"),
            "repo_url": f"https://github.com/{org}/{repo}",
            "org": org,
            "repo": repo,
            "branch": branch,
            "pinned_sha": pinned_sha,
            "source_path": m.group("path"),
            "array": current_array,
        })

    return results


def _git(repo_path: Path, *args: str, timeout: int = 30) -> str:
    result = subprocess.run(
        ["git", f"--git-dir={repo_path}", *args],
        capture_output=True, text=True, timeout=timeout,
    )
    return result.stdout.strip()


def _get_script_at_sha(repo_path: Path, sha: str) -> str | None:
    """Read get_all_manifests.sh at a specific commit SHA."""
    try:
        return _git(repo_path, "show", f"{sha}:get_all_manifests.sh")
    except Exception:
        return None


def collect_manifest_pins(store: Store, repo_path: Path) -> int:
    """Parse get_all_manifests.sh at each manifest-update PR and at HEAD.

    For each PR with is_manifest_update=1, reads the script at that merge_sha
    and extracts component pins.  Always includes the current HEAD as well.
    """
    # Get manifest-update PRs that haven't been processed yet
    existing_prs = {
        r["pr_number"]
        for r in store.conn.execute(
            "SELECT DISTINCT pr_number FROM component_manifest_pins WHERE pr_number IS NOT NULL"
        ).fetchall()
    }

    manifest_prs = store.conn.execute("""
        SELECT number, merge_sha, merged_at
        FROM merged_prs
        WHERE is_manifest_update = 1 AND merge_sha IS NOT NULL
        ORDER BY merged_at
    """).fetchall()

    count = 0

    for pr in manifest_prs:
        pr_num = pr["number"]
        if pr_num in existing_prs:
            continue

        sha = pr["merge_sha"]
        merged_at = pr["merged_at"]

        script = _get_script_at_sha(repo_path, sha)
        if not script:
            log.debug("Could not read get_all_manifests.sh at %s (PR #%d)", sha, pr_num)
            continue

        entries = _parse_manifest_entries(script)
        for entry in entries:
            if not entry["pinned_sha"]:
                continue
            store.upsert_manifest_pin(
                component=entry["component"],
                repo_url=entry["repo_url"],
                branch=entry["branch"],
                pinned_sha=entry["pinned_sha"],
                source_path=entry["source_path"],
                captured_at=merged_at,
                pr_number=pr_num,
            )
            count += 1

    # Always parse HEAD for the current baseline
    head_sha = _git(repo_path, "rev-parse", "HEAD")
    if head_sha:
        script = _get_script_at_sha(repo_path, head_sha)
        if script:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            entries = _parse_manifest_entries(script)
            for entry in entries:
                if not entry["pinned_sha"]:
                    continue
                store.upsert_manifest_pin(
                    component=entry["component"],
                    repo_url=entry["repo_url"],
                    branch=entry["branch"],
                    pinned_sha=entry["pinned_sha"],
                    source_path=entry["source_path"],
                    captured_at=now,
                    pr_number=None,
                )
                count += 1

    log.info("Stored %d manifest pin records", count)
    return count


def collect_manifest_deltas(store: Store) -> int:
    """Fetch upstream commit changelogs between consecutive SHA bumps.

    For each component where the pinned_sha changed between consecutive
    captured_at timestamps, calls the GitHub compare API to get the commits
    between old_sha and new_sha.
    """
    token = os.environ.get("GITHUB_TOKEN", "")

    # Find consecutive pin changes per component
    rows = store.conn.execute("""
        SELECT component, repo_url, pinned_sha, captured_at, pr_number
        FROM component_manifest_pins
        ORDER BY component, captured_at
    """).fetchall()

    # Group by component, find transitions
    transitions: list[dict] = []
    prev_by_component: dict[str, dict] = {}

    for row in rows:
        comp = row["component"]
        sha = row["pinned_sha"]
        prev = prev_by_component.get(comp)

        if prev and prev["pinned_sha"] != sha:
            transitions.append({
                "component": comp,
                "repo_url": row["repo_url"],
                "old_sha": prev["pinned_sha"],
                "new_sha": sha,
                "pr_number": row["pr_number"],
            })

        prev_by_component[comp] = dict(row)

    if not transitions:
        log.info("No manifest SHA transitions to fetch deltas for")
        return 0

    # Skip transitions already fetched
    existing = {
        (r["component"], r["old_sha"], r["new_sha"])
        for r in store.conn.execute(
            "SELECT component, old_sha, new_sha FROM manifest_sha_deltas"
        ).fetchall()
    }
    transitions = [
        t for t in transitions
        if (t["component"], t["old_sha"], t["new_sha"]) not in existing
    ]

    if not transitions:
        log.info("All manifest deltas already fetched")
        return 0

    if not token:
        log.warning(
            "GITHUB_TOKEN not set — skipping %d manifest delta fetches "
            "(unauthenticated limit is 60 req/hr, need %d). "
            "Set GITHUB_TOKEN to enable this.",
            len(transitions), len(transitions),
        )
        return 0

    log.info("Fetching %d manifest SHA deltas from GitHub...", len(transitions))

    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
    }

    count = 0
    with httpx.Client(base_url="https://api.github.com", headers=headers) as client:
        for t in transitions:
            repo_url = t["repo_url"]
            parts = repo_url.rstrip("/").split("/")
            owner, repo = parts[-2], parts[-1]

            try:
                resp = client.get(
                    f"/repos/{owner}/{repo}/compare/{t['old_sha']}...{t['new_sha']}",
                    timeout=15,
                )
                if resp.status_code == 404:
                    log.debug("Compare 404 for %s %s..%s (force push or repo mismatch)",
                              t["component"], t["old_sha"][:8], t["new_sha"][:8])
                    continue
                if resp.status_code == 403:
                    remaining = resp.headers.get("x-ratelimit-remaining", "?")
                    log.warning("GitHub API rate limited (remaining=%s); stopping delta fetch after %d/%d",
                                remaining, count, len(transitions))
                    break
                resp.raise_for_status()
                data = resp.json()
            except httpx.HTTPStatusError:
                log.debug("Failed to fetch compare for %s", t["component"], exc_info=True)
                continue

            commits_raw = data.get("commits", [])[:50]
            commits_json = json.dumps([
                {
                    "sha": c["sha"][:12],
                    "message": (c.get("commit", {}).get("message", "") or "")[:200],
                    "author": (c.get("commit", {}).get("author", {}).get("name", "")),
                    "date": c.get("commit", {}).get("author", {}).get("date", ""),
                }
                for c in commits_raw
            ])

            store.upsert_manifest_delta(
                component=t["component"],
                old_sha=t["old_sha"],
                new_sha=t["new_sha"],
                repo_url=repo_url,
                commit_count=data.get("total_commits", len(commits_raw)),
                commits_json=commits_json,
                pr_number=t["pr_number"],
            )
            count += 1

    log.info("Stored %d manifest SHA deltas", count)
    return count
