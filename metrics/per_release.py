"""Per-release metrics (DORA + CI) for v3.x+ releases."""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import datetime

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


def _percentiles(values: list[float], pcts: tuple = (50, 90)) -> dict[str, float | None]:
    if not values:
        return {f"p{p}": None for p in pcts}
    sorted_vals = sorted(values)
    result = {}
    for p in pcts:
        idx = min(int(len(sorted_vals) * p / 100), len(sorted_vals) - 1)
        result[f"p{p}"] = round(sorted_vals[idx], 1)
    return result


def _tag_to_downstream(tag: str) -> str:
    """Derive the downstream branch name from an upstream tag.

    v3.3.0       -> rhoai-3.3
    v3.4.0-ea.1  -> rhoai-3.4-ea.1
    """
    stripped = tag.lstrip("v")
    if "-" in stripped:
        base, suffix = stripped.split("-", 1)
        ver = base.rsplit(".", 1)[0]
        return f"rhoai-{ver}-{suffix}"
    ver = stripped.rsplit(".", 1)[0]
    return f"rhoai-{ver}"


def _parse_version(tag: str) -> tuple:
    """Parse a tag into a sortable tuple. Handles EA tags like v3.4.0-ea.2."""
    stripped = tag.lstrip("v")
    if "-ea." in stripped:
        base, ea_num = stripped.split("-ea.")
        parts = base.split(".")
        return tuple(int(p) for p in parts) + (0, int(ea_num))
    parts = stripped.split(".")
    return tuple(int(p) for p in parts) + (1, 0)


def compute(store: Store, min_version: str = "3.0.0") -> list[dict]:
    """Compute per-release metrics for releases >= min_version."""
    repo_name = "opendatahub-io/opendatahub-operator"
    downstream_repo = "red-hat-data-services/rhods-operator"

    all_releases = store.get_releases()
    all_prs = store.get_merged_prs(repo=repo_name, base_branch="main")
    all_cherry_picks = store.get_cherry_picks(repo=downstream_repo)
    downstream_branches = {b["name"] for b in store.get_downstream_branches()}
    release_by_tag = {r["tag"]: r for r in all_releases}

    min_ver = _parse_version(f"v{min_version}")
    eligible = [
        r for r in all_releases
        if not r["is_patch"] and _parse_version(r["tag"]) >= min_ver
    ]
    eligible.sort(key=lambda r: _parse_version(r["tag"]))

    # Build PR -> first tag mapping from branch_arrivals
    pr_to_tag: dict[int, str] = {}
    for pr in all_prs:
        arrivals = store.get_branch_arrivals(repo_name, pr["number"])
        for a in arrivals:
            if a["branch"].startswith("tag:"):
                pr_to_tag[pr["number"]] = a["branch"].removeprefix("tag:")
                break

    # Group PRs by their first tag
    prs_by_tag: dict[str, list[dict]] = defaultdict(list)
    for pr in all_prs:
        tag = pr_to_tag.get(pr["number"])
        if tag:
            prs_by_tag[tag].append(pr)

    # Cherry-picks grouped by downstream branch
    cp_by_branch: dict[str, int] = defaultdict(int)
    for cp in all_cherry_picks:
        cp_by_branch[cp["target_branch"]] += 1

    results: list[dict] = []

    for i, rel in enumerate(eligible):
        tag = rel["tag"]
        published = rel["published"]
        downstream_name = _tag_to_downstream(tag)
        has_downstream = downstream_name in downstream_branches

        # PRs in this release
        prs = prs_by_tag.get(tag, [])

        # Days since previous release
        days_since_prev = None
        if i > 0:
            prev_published = eligible[i - 1]["published"]
            h = _hours_between(prev_published, published)
            if h is not None:
                days_since_prev = round(h / 24, 1)

        # Lead time: PR merge -> release tag date
        lead_times = []
        for pr in prs:
            h = _hours_between(pr.get("merged_at"), published)
            if h is not None and h >= 0:
                lead_times.append(h)

        # Cycle time: first commit -> PR merge
        cycle_times = []
        for pr in prs:
            h = _hours_between(pr.get("first_commit_at"), pr.get("merged_at"))
            if h is not None and h > 0:
                cycle_times.append(h)

        # Cherry-picks on downstream branch
        cherry_picks = cp_by_branch.get(downstream_name, 0)

        # Patch releases
        ver = tag.lstrip("v").split("-")[0].rsplit(".", 1)[0]
        patches = [
            r for r in all_releases
            if r["is_patch"] and r["tag"].startswith(f"v{ver}.")
        ]
        has_patch = len(patches) > 0
        patch_hours = None
        if patches:
            first_patch = min(patches, key=lambda p: p["published"])
            patch_hours = _hours_between(published, first_patch["published"])
            if patch_hours is not None:
                patch_hours = round(patch_hours, 1)

        # Use downstream name as label when branch exists, else upstream version
        label = downstream_name if has_downstream else tag.lstrip("v").split("-")[0].rsplit(".", 1)[0]
        if rel["is_ea"]:
            label = downstream_name if has_downstream else tag.lstrip("v")

        results.append({
            "tag": tag,
            "label": label,
            "downstream_branch": downstream_name if has_downstream else None,
            "published": published,
            "is_ea": rel["is_ea"],
            "pr_count": len(prs),
            "days_since_previous": days_since_prev,
            "lead_time_p50": _percentiles(lead_times).get("p50"),
            "lead_time_p90": _percentiles(lead_times).get("p90"),
            "lead_time_mean": round(statistics.mean(lead_times), 1) if lead_times else None,
            "cycle_time_p50": _percentiles(cycle_times).get("p50"),
            "cycle_time_p90": _percentiles(cycle_times).get("p90"),
            "cycle_time_mean": round(statistics.mean(cycle_times), 1) if cycle_times else None,
            "cherry_picks": cherry_picks,
            "has_patch": has_patch,
            "patch_turnaround_hours": patch_hours,
        })

    return results
