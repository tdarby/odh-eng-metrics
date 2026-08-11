"""Mean Time to Recovery: how quickly failures are resolved."""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone

from store.db import Store


def _cutoff_iso(lookback_days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()


def _hours_between(iso_a: str | None, iso_b: str | None) -> float | None:
    if not iso_a or not iso_b:
        return None
    try:
        a = datetime.fromisoformat(iso_a.replace("Z", "+00:00"))
        b = datetime.fromisoformat(iso_b.replace("Z", "+00:00"))
        return (b - a).total_seconds() / 3600
    except Exception:
        return None


def _classify(hours: float | None) -> str:
    if hours is None:
        return "Insufficient data"
    if hours <= 1:
        return "Elite"
    if hours <= 24:
        return "High"
    if hours <= 168:  # 1 week
        return "Medium"
    return "Low"


def _percentiles(values: list[float], pcts: tuple = (50, 90)) -> dict[str, float | None]:
    if not values:
        return {f"p{p}": None for p in pcts}
    sorted_vals = sorted(values)
    result = {}
    for p in pcts:
        idx = min(int(len(sorted_vals) * p / 100), len(sorted_vals) - 1)
        result[f"p{p}"] = round(sorted_vals[idx], 1)
    return result


def _format_hours(hours: float | None) -> str:
    """Format hours in a human-friendly way."""
    if hours is None:
        return "N/A"
    if abs(hours) < 48:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"


def compute(store: Store, lookback_days: int = 365) -> dict:
    cutoff = _cutoff_iso(lookback_days)

    all_releases = store.get_releases()
    releases = [r for r in all_releases if (r.get("published") or "") >= cutoff]

    release_by_tag: dict[str, dict] = {r["tag"]: r for r in all_releases}
    patch_releases = [r for r in releases if r["is_patch"]]

    patch_turnarounds: list[float] = []
    details: list[dict] = []

    for patch in patch_releases:
        tag = patch["tag"]
        parts = tag.lstrip("v").split(".")
        if len(parts) >= 3:
            base_tag = f"v{parts[0]}.{parts[1]}.0"
            base = release_by_tag.get(base_tag)
            hours = _hours_between(
                base["published"] if base else None,
                patch["published"],
            )
            if hours is not None and hours >= 0:
                patch_turnarounds.append(hours)
            details.append({"patch": tag, "hours": _format_hours(hours)})

    reverts = store.get_reverts(repo="opendatahub-io/opendatahub-operator")
    reverts = [r for r in reverts if (r.get("date") or "") >= cutoff]
    cherry_picks = store.get_cherry_picks(repo="red-hat-data-services/rhods-operator")
    cherry_picks = [cp for cp in cherry_picks if (cp.get("merged_at") or "") >= cutoff]

    all_recovery_times = patch_turnarounds

    return {
        "patch_release_turnaround_hours": {
            "count": len(patch_turnarounds),
            "details": details,
            "mean": round(statistics.mean(patch_turnarounds), 1) if patch_turnarounds else None,
            **_percentiles(patch_turnarounds),
        },
        "reverts_pending_analysis": len(reverts),
        "cherry_picks_pending_analysis": len(cherry_picks),
        "overall_recovery_hours": {
            "count": len(all_recovery_times),
            "mean": round(statistics.mean(all_recovery_times), 1) if all_recovery_times else None,
            **_percentiles(all_recovery_times),
            "dora_classification": _classify(
                statistics.median(all_recovery_times) if all_recovery_times else None
            ),
        },
    }
