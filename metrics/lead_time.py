"""Lead Time for Changes: time from first commit to merge, and through the pipeline."""

from __future__ import annotations

import json
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


def _percentiles(values: list[float], pcts: tuple = (50, 75, 90)) -> dict[str, float | None]:
    if not values:
        return {f"p{p}": None for p in pcts}
    result = {}
    for p in pcts:
        idx = int(len(values) * p / 100)
        idx = min(idx, len(values) - 1)
        sorted_vals = sorted(values)
        result[f"p{p}"] = round(sorted_vals[idx], 1)
    return result


def compute(store: Store) -> dict:
    repo_name = "opendatahub-io/opendatahub-operator"
    prs = store.get_merged_prs(repo=repo_name, base_branch="main")

    pr_cycle_times: list[float] = []
    pr_review_times: list[float] = []
    to_stable: list[float] = []
    to_rhoai: list[float] = []
    to_release: list[float] = []

    for pr in prs:
        # Stage 1: first commit → merge
        ct = _hours_between(pr.get("first_commit_at"), pr.get("merged_at"))
        if ct is not None and ct > 0:
            pr_cycle_times.append(ct)

        # PR review time: created → merge
        rt = _hours_between(pr.get("created_at"), pr.get("merged_at"))
        if rt is not None and rt > 0:
            pr_review_times.append(rt)

        # Stages 2-4 from branch arrivals
        arrivals = store.get_branch_arrivals(repo_name, pr["number"])
        arrival_map = {a["branch"]: a["arrived_at"] for a in arrivals}

        stable_arrival = _hours_between(pr.get("merged_at"), arrival_map.get("stable"))
        if stable_arrival is not None and stable_arrival >= 0:
            to_stable.append(stable_arrival)

        rhoai_arrival = _hours_between(pr.get("merged_at"), arrival_map.get("rhoai"))
        if rhoai_arrival is not None and rhoai_arrival >= 0:
            to_rhoai.append(rhoai_arrival)

        # Find earliest tag arrival
        tag_arrivals = [(k, v) for k, v in arrival_map.items() if k.startswith("tag:")]
        if tag_arrivals:
            earliest_tag_date = min(v for _, v in tag_arrivals if v)
            tag_hours = _hours_between(pr.get("merged_at"), earliest_tag_date)
            if tag_hours is not None and tag_hours >= 0:
                to_release.append(tag_hours)

    jira_lead = _compute_jira_lead_time(store, prs)

    return {
        "pr_cycle_time_hours": {
            "count": len(pr_cycle_times),
            "mean": round(statistics.mean(pr_cycle_times), 1) if pr_cycle_times else None,
            **_percentiles(pr_cycle_times),
        },
        "pr_review_time_hours": {
            "count": len(pr_review_times),
            "mean": round(statistics.mean(pr_review_times), 1) if pr_review_times else None,
            **_percentiles(pr_review_times),
        },
        "to_stable_hours": {
            "count": len(to_stable),
            "mean": round(statistics.mean(to_stable), 1) if to_stable else None,
            **_percentiles(to_stable),
        },
        "to_rhoai_hours": {
            "count": len(to_rhoai),
            "mean": round(statistics.mean(to_rhoai), 1) if to_rhoai else None,
            **_percentiles(to_rhoai),
        },
        "to_release_hours": {
            "count": len(to_release),
            "mean": round(statistics.mean(to_release), 1) if to_release else None,
            **_percentiles(to_release),
        },
        "jira_issue_to_merge_hours": jira_lead,
    }


def _parse_json_field(value: str | None) -> list:
    if not value:
        return []
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []


def _compute_jira_lead_time(store: Store, prs: list[dict]) -> dict:
    """JIRA issue created -> PR merged lead time (idea to delivery).

    For PRs with multiple JIRA keys, uses the earliest issue creation date.
    """
    jira_issue_map = store.get_jira_issue_map()
    if not jira_issue_map:
        return {"count": 0, "mean": None, "p50": None, "p90": None, "by_type": []}

    all_hours: list[float] = []
    by_type: dict[str, list[float]] = defaultdict(list)

    for pr in prs:
        keys = _parse_json_field(pr.get("jira_keys"))
        if not keys:
            continue
        merged = pr.get("merged_at")
        if not merged:
            continue

        earliest_created = None
        issue_types: set[str] = set()
        for key in keys:
            issue = jira_issue_map.get(key)
            if not issue or not issue.get("created"):
                continue
            issue_types.add(issue.get("issue_type") or "Unknown")
            h = _hours_between(issue["created"], merged)
            if h is not None and h > 0:
                if earliest_created is None or h > earliest_created:
                    earliest_created = h

        if earliest_created is not None:
            all_hours.append(earliest_created)
            for itype in issue_types:
                by_type[itype].append(earliest_created)

    by_type_results = []
    for itype, hours in sorted(by_type.items()):
        by_type_results.append({
            "issue_type": itype,
            "count": len(hours),
            "mean": round(statistics.mean(hours), 1) if hours else None,
            **_percentiles(hours),
        })

    return {
        "count": len(all_hours),
        "mean": round(statistics.mean(all_hours), 1) if all_hours else None,
        **_percentiles(all_hours),
        "by_type": by_type_results,
    }
