"""Detect actual cherry-picks on downstream release branches -- git-first, zero API calls.

Only counts commits whose message contains "(cherry picked from commit" (the
standard output of `git cherry-pick -x`) or whose subject mentions
cherry-pick/backport/hotfix.  This avoids counting the thousands of
regular commits that arrive via branch syncs.
"""

from __future__ import annotations

import logging
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import git

from store.db import Store

log = logging.getLogger(__name__)

CHERRY_PICK_RE = re.compile(
    r"\(cherry picked from commit [0-9a-f]|cherry.?pick|backport|hotfix",
    re.IGNORECASE,
)


def _git(repo_path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", f"--git-dir={repo_path}", *args],
        capture_output=True, text=True, timeout=120,
    )
    return result.stdout.strip()


def collect_cherry_picks(
    store: Store,
    downstream_repo: git.Repo,
    cfg: dict,
    lookback_days: int = 365,
) -> int:
    """Find cherry-pick commits on downstream release branches via git log --grep."""
    branch_cfg = cfg["downstream"]["branches"]
    release_re = re.compile(branch_cfg["release_pattern"])
    ea_re = re.compile(branch_cfg.get("ea_pattern", "$^"))
    bot_prefixes = tuple(cfg["downstream"].get("bot_pr_prefixes", []))

    repo_path = Path(downstream_repo.common_dir)
    repo_key = f"{cfg['downstream']['owner']}/{cfg['downstream']['repo']}"
    since = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

    ref_output = _git(repo_path, "for-each-ref", "--format=%(refname:short)",
                       "refs/heads/", "refs/remotes/origin/")
    seen: set[str] = set()
    release_branches: list[tuple[str, str]] = []

    for ref_name in ref_output.split("\n"):
        if not ref_name.strip():
            continue
        short = ref_name.removeprefix("origin/")
        if short in seen:
            continue
        if release_re.match(short) or ea_re.match(short):
            seen.add(short)
            release_branches.append((short, ref_name))

    log.info("Checking %d downstream release branches for cherry-picks", len(release_branches))

    pr_re = re.compile(r"#(\d+)")
    count = 0

    for branch_name, ref_name in release_branches:
        # Use --grep to only find commits with cherry-pick markers in the
        # full commit message (subject + body).  --extended-regexp allows the
        # alternation pattern.
        log_output = _git(
            repo_path, "log", ref_name,
            f"--since={since}",
            "--format=%H|%aI|%ae|%s|%b",
            "--extended-regexp",
            "--grep=cherry picked from commit",
            "--grep=cherry.pick",
            "--grep=backport",
            "--grep=hotfix",
        )
        if not log_output:
            continue

        for line in log_output.split("\n"):
            if not line.strip():
                continue
            parts = line.split("|", 4)
            if len(parts) < 4:
                continue
            sha = parts[0]
            date = parts[1]
            author_email = parts[2]
            subject = parts[3]
            body = parts[4] if len(parts) > 4 else ""

            full_msg = subject + " " + body
            if not CHERRY_PICK_RE.search(full_msg):
                continue

            if subject.startswith(bot_prefixes):
                continue

            pr_match = pr_re.search(subject)
            pr_number = int(pr_match.group(1)) if pr_match else hash(sha) % 100000

            store.upsert_cherry_pick(
                repo=repo_key,
                pr_number=pr_number,
                target_branch=branch_name,
                title=subject[:200],
                author=author_email.split("@")[0],
                merged_at=date,
            )
            count += 1

    log.info("Found %d cherry-pick commits across downstream branches", count)
    return count
