"""Detect revert commits on upstream main."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

import git

from store.db import Store

log = logging.getLogger(__name__)

REVERT_RE = re.compile(r'^Revert "(.+)"', re.MULTILINE)
REVERT_SHA_RE = re.compile(r"This reverts commit ([0-9a-f]{40})")
PR_NUM_RE = re.compile(r"#(\d+)")


def _resolve_pr_from_sha(repo: git.Repo, sha: str) -> int | None:
    """Extract PR number from the subject of the reverted commit."""
    try:
        reverted_commit = repo.commit(sha)
        subject = reverted_commit.message.split("\n")[0]
        m = PR_NUM_RE.search(subject)
        return int(m.group(1)) if m else None
    except Exception:
        return None


def collect_reverts(
    store: Store,
    upstream_repo: git.Repo,
    branch: str = "main",
    lookback_days: int = 365,
) -> int:
    """Scan upstream main for revert commits and record them."""
    since = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    repo_name = "opendatahub-io/opendatahub-operator"

    count = 0
    try:
        ref = upstream_repo.refs[branch]
    except (IndexError, KeyError):
        for r in upstream_repo.refs:
            if r.name.endswith(f"/{branch}"):
                ref = r
                break
        else:
            log.warning("Branch %s not found in upstream repo", branch)
            return 0

    for commit in upstream_repo.iter_commits(ref, since=since):
        msg = commit.message
        m = REVERT_RE.match(msg)
        if not m:
            continue

        sha_match = REVERT_SHA_RE.search(msg)
        reverted_sha = sha_match.group(1) if sha_match else None

        reverted_pr = None
        if reverted_sha:
            reverted_pr = _resolve_pr_from_sha(upstream_repo, reverted_sha)

        store.upsert_revert(
            repo=repo_name,
            sha=commit.hexsha,
            date=commit.committed_datetime.isoformat(),
            reverted_sha=reverted_sha,
            message=msg.split("\n")[0][:200],
            reverted_pr=reverted_pr,
        )
        count += 1

    log.info("Found %d revert commits on %s", count, branch)
    return count
