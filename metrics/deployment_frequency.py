"""Deployment Frequency (DORA): how often we deliver changes at each pipeline stage."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from store.db import Store

DORA_BANDS = [
    ("Elite", "On-demand (multiple deploys per day)"),
    ("High", "Between once per day and once per week"),
    ("Medium", "Between once per week and once per month"),
    ("Low", "Less than once per month"),
]


def _cutoff_iso(lookback_days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()


def _bucket_by_month(items: list[dict], date_key: str) -> dict[str, int]:
    buckets: dict[str, int] = defaultdict(int)
    for item in items:
        dt = item.get(date_key)
        if not dt:
            continue
        month = dt[:7]
        buckets[month] += 1
    return dict(sorted(buckets.items()))


def _classify(avg_days_between: float | None) -> str:
    if avg_days_between is None:
        return "Insufficient data"
    if avg_days_between <= 1:
        return "Elite"
    if avg_days_between <= 7:
        return "High"
    if avg_days_between <= 30:
        return "Medium"
    return "Low"


def _avg_gap_days(dates: list[str]) -> float | None:
    if len(dates) < 2:
        return None
    sorted_dates = sorted(dates)
    gaps = []
    for i in range(1, len(sorted_dates)):
        d1 = datetime.fromisoformat(sorted_dates[i - 1].replace("Z", "+00:00"))
        d2 = datetime.fromisoformat(sorted_dates[i].replace("Z", "+00:00"))
        gaps.append((d2 - d1).total_seconds() / 86400)
    return sum(gaps) / len(gaps) if gaps else None


def compute(store: Store, lookback_days: int = 365) -> dict:
    cutoff = _cutoff_iso(lookback_days)
    all_releases = store.get_releases()
    releases = [r for r in all_releases if (r.get("published") or "") >= cutoff]

    upstream_prs = store.get_merged_prs(base_branch="main")
    prs = [p for p in upstream_prs if (p.get("merged_at") or "") >= cutoff]

    downstream_branches = store.get_downstream_branches()

    stable_releases = [r for r in releases if not r["is_ea"]]
    ea_releases = [r for r in releases if r["is_ea"]]

    release_dates = [r["published"] for r in stable_releases]
    pr_merge_dates = [pr["merged_at"] for pr in prs]

    return {
        "releases": {
            "total": len(stable_releases),
            "ea_total": len(ea_releases),
            "by_month": _bucket_by_month(stable_releases, "published"),
            "avg_gap_days": _avg_gap_days(release_dates),
            "dora_classification": _classify(_avg_gap_days(release_dates)),
        },
        "pr_merges": {
            "total": len(prs),
            "by_month": _bucket_by_month(prs, "merged_at"),
            "avg_gap_days": _avg_gap_days(pr_merge_dates),
            "dora_classification": _classify(_avg_gap_days(pr_merge_dates)),
        },
        "downstream_branches": {
            "total": len(downstream_branches),
            "ea_count": sum(1 for b in downstream_branches if b["is_ea"]),
            "branches": [b["name"] for b in downstream_branches],
        },
    }
