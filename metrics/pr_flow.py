"""PR flow analysis: time-to-release and cycle time distributions."""

from __future__ import annotations

from datetime import datetime

from store.db import Store

TIME_TO_RELEASE_BUCKETS = ["<1d", "1-7d", "7-14d", "14-30d", ">30d"]
CYCLE_TIME_BUCKETS = ["<1h", "1-8h", "8-24h", "1-3d", "3-7d", "1-2w", ">2w"]


def _hours_between(iso_a: str | None, iso_b: str | None) -> float | None:
    if not iso_a or not iso_b:
        return None
    try:
        a = datetime.fromisoformat(iso_a.replace("Z", "+00:00"))
        b = datetime.fromisoformat(iso_b.replace("Z", "+00:00"))
        return (b - a).total_seconds() / 3600
    except Exception:
        return None


def _ttr_bucket(hours: float) -> str:
    if hours < 24:
        return "<1d"
    if hours < 168:
        return "1-7d"
    if hours < 336:
        return "7-14d"
    if hours < 720:
        return "14-30d"
    return ">30d"


def _cycle_bucket(hours: float) -> str:
    if hours < 1:
        return "<1h"
    if hours < 8:
        return "1-8h"
    if hours < 24:
        return "8-24h"
    if hours < 72:
        return "1-3d"
    if hours < 168:
        return "3-7d"
    if hours < 336:
        return "1-2w"
    return ">2w"


def compute(store: Store) -> dict:
    """Compute PR flow distributions."""
    repo_name = "opendatahub-io/opendatahub-operator"
    prs = store.get_merged_prs(repo=repo_name, base_branch="main")

    ttr_counts = {b: 0 for b in TIME_TO_RELEASE_BUCKETS}
    cycle_counts = {b: 0 for b in CYCLE_TIME_BUCKETS}

    for pr in prs:
        arrivals = store.get_branch_arrivals(repo_name, pr["number"])
        for a in arrivals:
            if a["branch"].startswith("tag:"):
                h = _hours_between(pr.get("merged_at"), a.get("arrived_at"))
                if h is not None and h >= 0:
                    ttr_counts[_ttr_bucket(h)] += 1
                break

        h = _hours_between(pr.get("first_commit_at"), pr.get("merged_at"))
        if h is not None and h > 0:
            cycle_counts[_cycle_bucket(h)] += 1

    return {
        "time_to_release": [
            {"bucket": b, "count": ttr_counts[b]} for b in TIME_TO_RELEASE_BUCKETS
        ],
        "cycle_time": [
            {"bucket": b, "count": cycle_counts[b]} for b in CYCLE_TIME_BUCKETS
        ],
    }
