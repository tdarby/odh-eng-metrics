"""Detect AI-assisted commits by scanning git log for known trailers and markers."""

from __future__ import annotations

import logging
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from store.db import Store

log = logging.getLogger(__name__)

AI_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"Co-Authored-By:.*Claude", re.IGNORECASE), "Claude"),
    (re.compile(r"Assisted-By:.*Claude", re.IGNORECASE), "Claude"),
    (re.compile(r"Generated.{0,5}(with|by).*Claude", re.IGNORECASE), "Claude"),
    (re.compile(r"Co-Authored-By:.*Copilot", re.IGNORECASE), "Copilot"),
    (re.compile(r"Assisted-By:.*Copilot", re.IGNORECASE), "Copilot"),
    (re.compile(r"Made-with:\s*Cursor", re.IGNORECASE), "Cursor"),
    (re.compile(r"Assisted-By:.*Cursor", re.IGNORECASE), "Cursor"),
    (re.compile(r"Generated.{0,5}(with|by).*Cursor", re.IGNORECASE), "Cursor"),
    (re.compile(r"Co-Authored-By:.*OpenAI", re.IGNORECASE), "OpenAI"),
    (re.compile(r"Co-Authored-By:.*GPT", re.IGNORECASE), "OpenAI"),
    (re.compile(r"Generated.{0,5}(with|by).*Copilot", re.IGNORECASE), "Copilot"),
]

SEPARATOR = "---END---"


def _git(repo_path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", f"--git-dir={repo_path}", *args],
        capture_output=True, text=True, timeout=120,
    )
    return result.stdout.strip()


def collect_ai_commits(
    store: Store,
    repo_path: Path,
    repo_name: str,
    lookback_days: int = 365,
) -> int:
    """Scan all branches for commits with AI-assisted trailers."""
    since = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

    raw = _git(
        repo_path, "log", "--all",
        f"--since={since}",
        f"--format=%H|%aI|%s{SEPARATOR}%b{SEPARATOR}",
    )
    if not raw:
        return 0

    count = 0
    seen: set[tuple[str, str]] = set()

    for block in raw.split(f"{SEPARATOR}\n"):
        block = block.strip()
        if not block:
            continue

        parts = block.split(SEPARATOR)
        header = parts[0] if parts else ""
        body = parts[1] if len(parts) > 1 else ""

        header_fields = header.split("|", 2)
        if len(header_fields) < 3:
            continue

        sha, date, subject = header_fields
        full_text = subject + "\n" + body

        for pattern, tool in AI_PATTERNS:
            if pattern.search(full_text):
                key = (sha, tool)
                if key in seen:
                    continue
                seen.add(key)
                store.upsert_ai_commit(
                    repo=repo_name,
                    sha=sha,
                    date=date,
                    message=subject,
                    tool=tool,
                )
                count += 1

    log.info("Found %d AI-assisted commit markers in %s", count, repo_name)
    return count
