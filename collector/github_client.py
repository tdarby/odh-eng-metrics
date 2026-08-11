"""Thin wrapper around the GitHub REST API with pagination and rate-limit handling.

Only used for the Releases endpoint (to get the prerelease flag).
Everything else is extracted from bare git clones.

Set GITHUB_TOKEN env var for 5000 req/hr; without it you get 60 req/hr.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Iterator

import httpx

log = logging.getLogger(__name__)

BASE = "https://api.github.com"
PER_PAGE = 100


def _headers() -> dict[str, str]:
    h = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        h["Authorization"] = f"Bearer {token}"
    else:
        log.warning(
            "GITHUB_TOKEN not set -- API calls limited to 60/hr. "
            "Set it to avoid rate limits on the releases endpoint."
        )
    return h


def _check_rate_limit(resp: httpx.Response) -> None:
    if resp.status_code == 403:
        remaining = resp.headers.get("x-ratelimit-remaining", "?")
        raise RuntimeError(
            f"GitHub API rate-limited (remaining={remaining}). "
            "Set GITHUB_TOKEN env var or wait for reset."
        )


def paginate(url: str, params: dict[str, Any] | None = None) -> Iterator[dict]:
    """Yield every item across all pages of a GitHub list endpoint."""
    params = dict(params or {})
    params.setdefault("per_page", PER_PAGE)
    page = 1

    with httpx.Client(headers=_headers(), timeout=30) as client:
        while True:
            params["page"] = page
            resp = client.get(url, params=params)
            _check_rate_limit(resp)
            resp.raise_for_status()
            items = resp.json()
            if not items:
                break
            yield from items
            if len(items) < PER_PAGE:
                break
            page += 1


def list_releases(owner: str, repo: str) -> list[dict]:
    """Fetch all GitHub releases (newest first)."""
    url = f"{BASE}/repos/{owner}/{repo}/releases"
    return list(paginate(url))
