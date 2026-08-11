"""Change Failure Rate: proportion of changes that cause failures."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from store.db import Store


def _cutoff_iso(lookback_days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()


def _classify(rate: float | None) -> str:
    if rate is None:
        return "Insufficient data"
    if rate <= 0.05:
        return "Elite"
    if rate <= 0.10:
        return "High"
    if rate <= 0.15:
        return "Medium"
    return "Low"


def compute(store: Store, lookback_days: int = 365) -> dict:
    cutoff = _cutoff_iso(lookback_days)

    all_releases = store.get_releases()
    releases = [r for r in all_releases if (r.get("published") or "") >= cutoff]

    reverts = store.get_reverts(repo="opendatahub-io/opendatahub-operator")
    reverts = [r for r in reverts if (r.get("date") or "") >= cutoff]

    cherry_picks = store.get_cherry_picks(repo="red-hat-data-services/rhods-operator")
    cherry_picks = [cp for cp in cherry_picks if (cp.get("merged_at") or "") >= cutoff]

    # Unique cherry-pick target branches as a proxy for "incidents fixed"
    cherry_pick_branches = set(cp["target_branch"] for cp in cherry_picks)

    patch_releases = [r for r in releases if r["is_patch"]]
    stable_releases = [r for r in releases if not r["is_ea"] and not r["is_patch"]]

    # DORA CFR = failure-causing changes / total changes
    # Denominator: total PRs merged to main (total changes delivered)
    all_prs = store.get_merged_prs(base_branch="main")
    total_changes = len([p for p in all_prs if (p.get("merged_at") or "") >= cutoff])

    # Numerator: distinct failure signals
    #   - patch releases (each = 1 failure event requiring a hotfix release)
    #   - reverts on main (each = 1 bad change)
    #   - cherry-pick branches (each branch with cherry-picks = 1 incident)
    failure_events = len(patch_releases) + len(reverts) + len(cherry_pick_branches)

    if total_changes > 0:
        rate = failure_events / total_changes
    else:
        rate = None

    return {
        "total_changes": total_changes,
        "total_stable_releases": len(stable_releases),
        "patch_releases": len(patch_releases),
        "patch_release_list": [r["tag"] for r in patch_releases],
        "reverts_on_main": len(reverts),
        "revert_list": [{"sha": r["sha"][:12], "date": r["date"], "message": r["message"]} for r in reverts],
        "human_cherry_picks": len(cherry_picks),
        "cherry_pick_branches": len(cherry_pick_branches),
        "cherry_pick_list": [
            {"pr": cp["pr_number"], "branch": cp["target_branch"], "title": cp["title"][:80]}
            for cp in cherry_picks
        ],
        "total_failure_events": failure_events,
        "rate": round(rate, 4) if rate is not None else None,
        "rate_pct": f"{rate * 100:.1f}%" if rate is not None else "N/A",
        "dora_classification": _classify(rate),
    }
