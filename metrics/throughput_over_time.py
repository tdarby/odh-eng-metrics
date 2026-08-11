"""Monthly throughput metrics: PRs, releases, cherry-picks, reverts over time."""

from __future__ import annotations

from collections import defaultdict

from store.db import Store


def _month(iso: str | None) -> str | None:
    """Extract YYYY-MM from an ISO timestamp."""
    if not iso:
        return None
    return iso[:7]


def compute(store: Store) -> dict:
    """Compute monthly aggregates for throughput trends."""
    prs = store.get_merged_prs(base_branch="main")
    releases = store.get_releases()
    cherry_picks = store.get_cherry_picks()
    reverts = store.get_reverts()

    prs_by_month: dict[str, int] = defaultdict(int)
    for pr in prs:
        m = _month(pr.get("merged_at"))
        if m:
            prs_by_month[m] += 1

    releases_by_month: dict[str, dict[str, int]] = defaultdict(lambda: {"stable": 0, "ea": 0, "patch": 0})
    for r in releases:
        m = _month(r.get("published"))
        if not m:
            continue
        if r["is_ea"]:
            releases_by_month[m]["ea"] += 1
        elif r["is_patch"]:
            releases_by_month[m]["patch"] += 1
        else:
            releases_by_month[m]["stable"] += 1

    cp_by_month: dict[str, int] = defaultdict(int)
    for cp in cherry_picks:
        m = _month(cp.get("merged_at"))
        if m:
            cp_by_month[m] += 1

    reverts_by_month: dict[str, int] = defaultdict(int)
    for rv in reverts:
        m = _month(rv.get("date"))
        if m:
            reverts_by_month[m] += 1

    all_months = sorted(set(prs_by_month) | set(releases_by_month) | set(cp_by_month) | set(reverts_by_month))

    months = []
    for m in all_months:
        rel = releases_by_month.get(m, {"stable": 0, "ea": 0, "patch": 0})
        months.append({
            "month": m,
            "prs_merged": prs_by_month.get(m, 0),
            "releases_stable": rel["stable"],
            "releases_ea": rel["ea"],
            "releases_patch": rel["patch"],
            "cherry_picks": cp_by_month.get(m, 0),
            "reverts": reverts_by_month.get(m, 0),
        })

    return {"months": months}
