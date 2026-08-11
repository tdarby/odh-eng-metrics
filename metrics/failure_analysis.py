"""Failure analysis: cherry-picks by branch, monthly failure trends, revert details."""

from __future__ import annotations

from collections import defaultdict

from store.db import Store


def _month(iso: str | None) -> str | None:
    if not iso:
        return None
    return iso[:7]


def compute(store: Store) -> dict:
    """Compute failure analysis metrics."""
    cherry_picks = store.get_cherry_picks()
    reverts = store.get_reverts()

    cp_by_branch: dict[str, int] = defaultdict(int)
    cp_by_month: dict[str, int] = defaultdict(int)
    for cp in cherry_picks:
        cp_by_branch[cp["target_branch"]] += 1
        m = _month(cp.get("merged_at"))
        if m:
            cp_by_month[m] += 1

    reverts_by_month: dict[str, int] = defaultdict(int)
    revert_details = []
    for rv in reverts:
        m = _month(rv.get("date"))
        if m:
            reverts_by_month[m] += 1
        revert_details.append({
            "date": rv.get("date", "")[:10],
            "message": rv.get("message", "")[:120],
        })

    all_months = sorted(set(cp_by_month) | set(reverts_by_month))

    return {
        "cherry_picks_by_branch": [
            {"branch": b, "count": c}
            for b, c in sorted(cp_by_branch.items(), key=lambda x: -x[1])
        ],
        "monthly_failures": [
            {
                "month": m,
                "cherry_picks": cp_by_month.get(m, 0),
                "reverts": reverts_by_month.get(m, 0),
            }
            for m in all_months
        ],
        "revert_details": revert_details,
    }
