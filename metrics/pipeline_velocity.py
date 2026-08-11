"""Release pipeline velocity: stage durations from PR merge through downstream propagation."""

from __future__ import annotations

from datetime import datetime

from metrics.per_release import _parse_version, _tag_to_downstream
from store.db import Store


def _hours_between(iso_a: str | None, iso_b: str | None) -> float | None:
    if not iso_a or not iso_b:
        return None
    try:
        a = datetime.fromisoformat(iso_a.replace("Z", "+00:00"))
        b = datetime.fromisoformat(iso_b.replace("Z", "+00:00"))
        return (b - a).total_seconds() / 3600
    except Exception:
        return None


def compute(store: Store, min_version: str = "3.0.0") -> list[dict]:
    """Compute pipeline stage durations per release."""
    repo_name = "opendatahub-io/opendatahub-operator"
    all_releases = store.get_releases()
    all_prs = store.get_merged_prs(repo=repo_name, base_branch="main")
    downstream_branches = {b["name"]: b for b in store.get_downstream_branches()}

    min_ver = _parse_version(f"v{min_version}")
    eligible = [
        r for r in all_releases
        if not r["is_patch"] and _parse_version(r["tag"]) >= min_ver
    ]
    eligible.sort(key=lambda r: _parse_version(r["tag"]))

    # Build PR -> first tag mapping
    pr_to_tag: dict[int, str] = {}
    for pr in all_prs:
        arrivals = store.get_branch_arrivals(repo_name, pr["number"])
        for a in arrivals:
            if a["branch"].startswith("tag:"):
                pr_to_tag[pr["number"]] = a["branch"].removeprefix("tag:")
                break

    results = []
    for rel in eligible:
        tag = rel["tag"]
        published = rel["published"]
        downstream_name = _tag_to_downstream(tag)

        # Find earliest PR merge date for PRs in this release
        release_prs = [pr for pr in all_prs if pr_to_tag.get(pr["number"]) == tag]
        if not release_prs:
            continue

        earliest_merge = min(
            (pr["merged_at"] for pr in release_prs if pr.get("merged_at")),
            default=None,
        )

        accumulation_hours = _hours_between(earliest_merge, published)
        accumulation_days = round(accumulation_hours / 24, 1) if accumulation_hours else None

        # Downstream propagation: tag date -> downstream branch first commit date
        ds_branch = downstream_branches.get(downstream_name)
        downstream_days = None
        if ds_branch and ds_branch.get("first_commit_date"):
            ds_hours = _hours_between(published, ds_branch["first_commit_date"])
            if ds_hours is not None:
                downstream_days = round(ds_hours / 24, 1)

        has_downstream = downstream_name in downstream_branches
        label = downstream_name if has_downstream else tag.lstrip("v").split("-")[0].rsplit(".", 1)[0]
        if rel["is_ea"]:
            label = downstream_name if has_downstream else tag.lstrip("v")

        results.append({
            "tag": tag,
            "label": label,
            "published": published,
            "accumulation_days": accumulation_days,
            "downstream_days": downstream_days,
        })

    return results
