"""JIRA collection analytics — standalone metrics and cross-referenced intelligence.

Two layers:
  1. Standalone analytics (JIRA-only): base lifecycle + specialized analyzers
  2. Bug Bash Intelligence (JIRA x PR x CI): cross-referenced metrics,
     nonfixable root-cause analysis, automation gap analysis, recommendations
"""

from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from store.db import Store

log = logging.getLogger(__name__)


def _parse_json_field(value: str | None) -> list:
    if not value:
        return []
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []


def _parse_iso(date_str: str | None) -> datetime | None:
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _percentiles(values: list[float], pcts: list[float] = [50, 90]) -> dict[str, float | None]:
    if not values:
        return {f"p{int(p)}": None for p in pcts}
    s = sorted(values)
    result = {}
    for p in pcts:
        idx = int(len(s) * p / 100)
        idx = min(idx, len(s) - 1)
        result[f"p{int(p)}"] = round(s[idx], 1)
    return result


def compute_base_analytics(issues: list[dict]) -> dict:
    """Compute base analytics applicable to any JIRA collection."""
    total = len(issues)
    if total == 0:
        return {"total": 0, "empty": True}

    now = datetime.now(timezone.utc)

    # Status distribution
    status_counts: Counter[str] = Counter()
    status_cat_counts: Counter[str] = Counter()
    for issue in issues:
        status_counts[issue.get("status") or "Unknown"] += 1
        status_cat_counts[issue.get("status_category") or "Unknown"] += 1

    # Type distribution
    type_counts: Counter[str] = Counter()
    for issue in issues:
        type_counts[issue.get("issue_type") or "Unknown"] += 1

    # Priority distribution
    priority_counts: Counter[str] = Counter()
    for issue in issues:
        priority_counts[issue.get("priority") or "Unknown"] += 1

    # Assignee distribution
    assignee_counts: Counter[str] = Counter()
    for issue in issues:
        assignee_counts[issue.get("assignee") or "Unassigned"] += 1

    # Project distribution (derived from issue key prefix)
    project_counts: Counter[str] = Counter()
    for issue in issues:
        key = issue.get("key", "")
        project_counts[key.rsplit("-", 1)[0] if "-" in key else "Unknown"] += 1

    # Component distribution
    component_counts: Counter[str] = Counter()
    for issue in issues:
        for comp in _parse_json_field(issue.get("components")):
            component_counts[comp] += 1

    # Resolution metrics
    done_count = status_cat_counts.get("Done", 0)
    resolution_rate = round(done_count / total * 100, 1) if total else 0

    resolution_hours: list[float] = []
    for issue in issues:
        created = _parse_iso(issue.get("created"))
        resolved = _parse_iso(issue.get("resolved"))
        if created and resolved:
            hours = (resolved - created).total_seconds() / 3600
            if hours >= 0:
                resolution_hours.append(hours)

    resolution_stats = _percentiles(resolution_hours)
    resolution_stats["mean"] = (
        round(sum(resolution_hours) / len(resolution_hours), 1)
        if resolution_hours else None
    )
    resolution_stats["count"] = len(resolution_hours)

    # Aging for open issues
    open_ages_days: list[float] = []
    for issue in issues:
        cat = issue.get("status_category")
        if cat == "Done":
            continue
        created = _parse_iso(issue.get("created"))
        if created:
            age = (now - created).total_seconds() / 86400
            open_ages_days.append(age)

    aging_stats = _percentiles(open_ages_days)
    aging_stats["max"] = round(max(open_ages_days), 1) if open_ages_days else None
    aging_stats["count"] = len(open_ages_days)

    # Weekly resolution throughput
    weekly_resolved: Counter[str] = Counter()
    for issue in issues:
        resolved = _parse_iso(issue.get("resolved"))
        if resolved:
            iso_year, iso_week, _ = resolved.isocalendar()
            weekly_resolved[f"{iso_year}-W{iso_week:02d}"] += 1

    throughput = [
        {"week": w, "resolved": c}
        for w, c in sorted(weekly_resolved.items())
    ]

    return {
        "total": total,
        "empty": False,
        "project_distribution": _counter_to_list(project_counts),
        "status_distribution": _counter_to_list(status_counts),
        "status_category_distribution": _counter_to_list(status_cat_counts),
        "type_distribution": _counter_to_list(type_counts),
        "priority_distribution": _counter_to_list(priority_counts),
        "assignee_distribution": _counter_to_list(assignee_counts),
        "component_distribution": _counter_to_list(component_counts),
        "resolution_rate": resolution_rate,
        "resolution_time_hours": resolution_stats,
        "open_issue_aging_days": aging_stats,
        "weekly_throughput": throughput,
    }


def _counter_to_list(counter: Counter) -> list[dict]:
    total = sum(counter.values())
    return [
        {"name": name, "count": count, "pct": round(count / total * 100, 1) if total else 0}
        for name, count in counter.most_common()
    ]


# ---------------------------------------------------------------------------
# Extensible analyzer pattern
# ---------------------------------------------------------------------------

def analyze_bug_bash(issues: list[dict], collection_cfg: dict | None = None) -> dict:
    """Specialized analysis for the AI Bug Bash event.

    Taxonomy (from the event document):
      Triage:   ai-triaged -> ai-fixable | ai-nonfixable
      Outcomes: ai-fully-automated | ai-accelerated-fix |
                ai-could-not-fix | ai-verification-failed |
                regressions-found
    """
    if not issues:
        return {"available": False}

    total = len(issues)

    TRIAGE_LABELS = {"ai-triaged"}
    FIXABILITY_LABELS = {"ai-fixable", "ai-nonfixable"}
    SUCCESS_LABELS = {"ai-fully-automated", "ai-accelerated-fix"}
    FAILURE_LABELS = {"ai-could-not-fix", "ai-verification-failed"}
    OUTCOME_LABELS = SUCCESS_LABELS | FAILURE_LABELS | {"regressions-found"}

    triaged = []
    fixable = []
    nonfixable = []
    outcome_counts: Counter[str] = Counter()
    no_outcome_yet: list[dict] = []

    for issue in issues:
        issue_labels = set(_parse_json_field(issue.get("labels")))

        if issue_labels & TRIAGE_LABELS:
            triaged.append(issue)
        if "ai-fixable" in issue_labels:
            fixable.append(issue)
        if "ai-nonfixable" in issue_labels:
            nonfixable.append(issue)

        matched_outcomes = issue_labels & OUTCOME_LABELS
        if matched_outcomes:
            for lbl in matched_outcomes:
                outcome_counts[lbl] += 1
        elif issue_labels & {"ai-fixable"}:
            no_outcome_yet.append(issue)

    # Triage funnel
    triage_funnel = [
        {"stage": "ai-triaged", "count": len(triaged),
         "pct": round(len(triaged) / total * 100, 1) if total else 0},
        {"stage": "ai-fixable", "count": len(fixable),
         "pct": round(len(fixable) / total * 100, 1) if total else 0},
        {"stage": "ai-nonfixable", "count": len(nonfixable),
         "pct": round(len(nonfixable) / total * 100, 1) if total else 0},
    ]

    # Outcome distribution (of issues that have reached an outcome)
    outcome_total = sum(outcome_counts.values())
    outcomes = []
    for lbl in ["ai-fully-automated", "ai-accelerated-fix",
                "ai-could-not-fix", "ai-verification-failed",
                "regressions-found"]:
        count = outcome_counts.get(lbl, 0)
        outcomes.append({
            "label": lbl,
            "count": count,
            "pct_of_outcomes": round(count / outcome_total * 100, 1) if outcome_total else 0,
            "pct_of_fixable": round(count / len(fixable) * 100, 1) if fixable else 0,
        })

    # Key rates
    success_count = sum(outcome_counts.get(l, 0) for l in SUCCESS_LABELS)
    failure_count = sum(outcome_counts.get(l, 0) for l in FAILURE_LABELS)
    automated_count = outcome_counts.get("ai-fully-automated", 0)
    automation_rate = round(automated_count / len(fixable) * 100, 1) if fixable else 0
    fixable_completion = round(outcome_total / len(fixable) * 100, 1) if fixable else 0

    # Severity profile
    severity_profile: Counter[str] = Counter()
    for issue in issues:
        severity_profile[issue.get("priority") or "Unknown"] += 1

    # Who fixed (assignee of resolved issues)
    fixer_counts: Counter[str] = Counter()
    for issue in issues:
        if issue.get("status_category") == "Done":
            fixer_counts[issue.get("assignee") or "Unassigned"] += 1

    # Regressions introduced
    regression_count = outcome_counts.get("regressions-found", 0)

    # Per-project breakdown
    project_breakdown = _bug_bash_by_project(issues, SUCCESS_LABELS, FAILURE_LABELS, OUTCOME_LABELS)

    return {
        "available": True,
        "triage_funnel": triage_funnel,
        "outcomes": outcomes,
        "summary": {
            "triaged": len(triaged),
            "fixable": len(fixable),
            "nonfixable": len(nonfixable),
            "outcomes_reached": outcome_total,
            "awaiting_outcome": len(no_outcome_yet),
            "ai_success_count": success_count,
            "ai_failure_count": failure_count,
            "ai_automated_count": automated_count,
            "automation_rate": automation_rate,
            "fixable_completion_pct": fixable_completion,
            "regressions": regression_count,
        },
        "severity_profile": _counter_to_list(severity_profile),
        "top_fixers": _counter_to_list(fixer_counts),
        "by_project": project_breakdown,
    }


def _bug_bash_by_project(
    issues: list[dict],
    success_labels: set[str],
    failure_labels: set[str],
    outcome_labels: set[str],
) -> list[dict]:
    """Compute bug bash funnel metrics broken down by JIRA project."""
    by_proj: dict[str, list[dict]] = defaultdict(list)
    for issue in issues:
        key = issue.get("key", "")
        proj = key.rsplit("-", 1)[0] if "-" in key else "Unknown"
        by_proj[proj].append(issue)

    results = []
    for proj, proj_issues in sorted(by_proj.items(), key=lambda x: -len(x[1])):
        total = len(proj_issues)
        triaged = fixable = nonfixable = automated = accelerated = 0

        for issue in proj_issues:
            labels = set(_parse_json_field(issue.get("labels")))
            if "ai-triaged" in labels:
                triaged += 1
            if "ai-fixable" in labels:
                fixable += 1
            if "ai-nonfixable" in labels:
                nonfixable += 1
            if "ai-fully-automated" in labels:
                automated += 1
            if "ai-accelerated-fix" in labels:
                accelerated += 1

        results.append({
            "project": proj,
            "total": total,
            "triaged": triaged,
            "fixable": fixable,
            "nonfixable": nonfixable,
            "automated": automated,
            "accelerated": accelerated,
            "automation_rate": round(automated / fixable * 100, 1) if fixable else 0,
            "accelerated_rate": round(accelerated / fixable * 100, 1) if fixable else 0,
        })
    return results


ANALYZERS: dict[str, Any] = {
    "bug-bash": analyze_bug_bash,
}


def compute_collection_analytics(
    issues: list[dict],
    collection_cfg: dict | None = None,
) -> dict:
    """Compute full analytics for a collection: base + optional specialized."""
    result = compute_base_analytics(issues)

    analyzer_name = (collection_cfg or {}).get("analyzer")
    if analyzer_name and analyzer_name in ANALYZERS:
        log.info("Running specialized analyzer: %s", analyzer_name)
        result["specialized"] = ANALYZERS[analyzer_name](issues, collection_cfg)
        result["analyzer"] = analyzer_name

    return result


# ---------------------------------------------------------------------------
# Bug Bash Intelligence — cross-referenced analysis layer
# ---------------------------------------------------------------------------

_NONFIXABLE_THEMES: dict[str, list[str]] = {
    "no test coverage": [
        "no test", "no e2e", "missing test", "untested", "no coverage",
        "can't verify", "cannot verify", "manual verification",
    ],
    "multi-service / cross-repo": [
        "cross-repo", "multi-service", "upstream dependency", "integration",
        "another repo", "different repo", "external service",
    ],
    "UI / visual": [
        "ui", "frontend", "css", "visual regression", "screenshot",
        "browser", "dashboard ui",
    ],
    "flaky / non-deterministic": [
        "flaky", "intermittent", "race condition", "timing", "non-deterministic",
        "sometimes fails", "concurrency", "deadlock",
    ],
    "insufficient context": [
        "no reproduction", "cannot reproduce", "unclear", "need more info",
        "missing context", "steps to reproduce",
    ],
    "infrastructure / environment": [
        "cluster", "network", "timeout", "cloud", "provisioning",
        "infra", "environment-specific", "requires cluster",
    ],
    "complex state machine": [
        "state machine", "complex logic", "multi-step", "workflow",
        "reconciliation loop", "controller logic",
    ],
}


def _scan_text_for_themes(text: str) -> list[str]:
    """Find which nonfixability themes appear in a text blob."""
    if not text:
        return []
    lower = text.lower()
    found = []
    for theme, keywords in _NONFIXABLE_THEMES.items():
        if any(kw in lower for kw in keywords):
            found.append(theme)
    return found


def _issue_text_blob(issue: dict) -> str:
    """Combine summary + description + comment bodies for text scanning."""
    parts = [issue.get("summary") or ""]
    desc = issue.get("description")
    if desc:
        parts.append(desc)
    raw_comments = issue.get("comments")
    if raw_comments:
        try:
            comments = json.loads(raw_comments) if isinstance(raw_comments, str) else raw_comments
            for c in comments:
                parts.append(c.get("body", ""))
        except (json.JSONDecodeError, TypeError):
            pass
    return "\n".join(parts)


def _join_issues_to_prs(
    issues: list[dict], all_prs: list[dict],
) -> dict[str, list[dict]]:
    """Map JIRA keys -> list of PRs that reference them."""
    key_set = {i["key"] for i in issues}
    result: dict[str, list[dict]] = defaultdict(list)
    for pr in all_prs:
        raw_keys = pr.get("jira_keys")
        if not raw_keys:
            continue
        try:
            pr_keys = json.loads(raw_keys) if isinstance(raw_keys, str) else raw_keys
        except (json.JSONDecodeError, TypeError):
            continue
        for k in pr_keys:
            if k in key_set:
                result[k].append(pr)
    return dict(result)


def _pr_ci_builds(
    prs: list[dict], all_builds: list[dict],
) -> list[dict]:
    """Return CI builds for a set of PRs."""
    pr_nums = {pr["number"] for pr in prs}
    return [b for b in all_builds if b.get("pr_number") in pr_nums]


def _first_build_per_pr(builds: list[dict]) -> dict[int, dict]:
    """For each PR, return the chronologically first CI build."""
    by_pr: dict[int, list[dict]] = defaultdict(list)
    for b in builds:
        by_pr[b["pr_number"]].append(b)
    result = {}
    for pr_num, pr_builds in by_pr.items():
        pr_builds.sort(key=lambda b: b.get("started_at") or "")
        result[pr_num] = pr_builds[0]
    return result


def _analyze_nonfixable(
    nonfixable_issues: list[dict],
    fixable_issues: list[dict],
    risk_scores: list[dict],
) -> dict:
    """Root-cause analysis for ai-nonfixable issues.

    Clusters by component, priority, type, extracts keyword themes from
    description + comments, and contrasts with ai-fixable cohort.
    """
    if not nonfixable_issues:
        return {"available": False, "count": 0}

    total_nf = len(nonfixable_issues)
    total_fix = len(fixable_issues)

    # Component distribution
    nf_components: Counter[str] = Counter()
    fix_components: Counter[str] = Counter()
    for i in nonfixable_issues:
        for c in _parse_json_field(i.get("components")):
            nf_components[c] += 1
    for i in fixable_issues:
        for c in _parse_json_field(i.get("components")):
            fix_components[c] += 1

    # Priority distribution
    nf_priority: Counter[str] = Counter()
    for i in nonfixable_issues:
        nf_priority[i.get("priority") or "Unknown"] += 1

    # Type distribution
    nf_type: Counter[str] = Counter()
    for i in nonfixable_issues:
        nf_type[i.get("issue_type") or "Unknown"] += 1

    # Theme extraction from description + comments
    theme_counter: Counter[str] = Counter()
    issue_themes: list[dict] = []
    for issue in nonfixable_issues:
        blob = _issue_text_blob(issue)
        themes = _scan_text_for_themes(blob)
        theme_counter.update(themes)
        issue_themes.append({"key": issue["key"], "summary": issue.get("summary"), "themes": themes})

    themes_list = [
        {"theme": t, "count": c, "pct": round(c / total_nf * 100, 1)}
        for t, c in theme_counter.most_common()
    ]

    # Contrast with fixable — which components are disproportionately nonfixable?
    component_contrast = []
    all_comp_names = set(nf_components) | set(fix_components)
    for comp in sorted(all_comp_names):
        nf_count = nf_components.get(comp, 0)
        fix_count = fix_components.get(comp, 0)
        nf_pct = round(nf_count / total_nf * 100, 1) if total_nf else 0
        fix_pct = round(fix_count / total_fix * 100, 1) if total_fix else 0
        component_contrast.append({
            "component": comp,
            "nonfixable_count": nf_count, "nonfixable_pct": nf_pct,
            "fixable_count": fix_count, "fixable_pct": fix_pct,
            "overrepresented": nf_pct > fix_pct * 1.5 if fix_pct > 0 else nf_count > 0,
        })
    component_contrast.sort(key=lambda x: x["nonfixable_count"], reverse=True)

    # Code risk cross-reference
    risk_by_comp = {}
    for r in risk_scores:
        comp = r.get("component")
        if comp:
            risk_by_comp.setdefault(comp, []).append(r.get("risk_score", 0))
    nf_risk_comps = []
    for comp in nf_components:
        scores = risk_by_comp.get(comp, [])
        if scores:
            nf_risk_comps.append({
                "component": comp,
                "nonfixable_count": nf_components[comp],
                "avg_risk_score": round(sum(scores) / len(scores), 2),
                "high_risk_functions": sum(1 for s in scores if s >= 7.0),
            })
    nf_risk_comps.sort(key=lambda x: x.get("avg_risk_score", 0), reverse=True)

    return {
        "available": True,
        "count": total_nf,
        "by_component": _counter_to_list(nf_components),
        "by_priority": _counter_to_list(nf_priority),
        "by_type": _counter_to_list(nf_type),
        "themes": themes_list,
        "issue_details": issue_themes,
        "component_contrast": component_contrast,
        "code_risk_overlap": nf_risk_comps,
    }


def _analyze_acceleration_gap(
    accelerated_issues: list[dict],
    automated_issues: list[dict],
    issue_to_prs: dict[str, list[dict]],
    all_builds: list[dict],
    all_test_results: list[dict],
    all_failure_msgs: list[dict],
    risk_scores: list[dict],
) -> dict:
    """Gap analysis: what separates accelerated-fix from fully-automated?"""
    if not accelerated_issues:
        return {"available": False, "count": 0}

    # Collect PRs for each group
    accel_prs = []
    for issue in accelerated_issues:
        accel_prs.extend(issue_to_prs.get(issue["key"], []))
    auto_prs = []
    for issue in automated_issues:
        auto_prs.extend(issue_to_prs.get(issue["key"], []))

    accel_builds = _pr_ci_builds(accel_prs, all_builds)
    auto_builds = _pr_ci_builds(auto_prs, all_builds)

    # First-pass CI results for accelerated-fix PRs
    accel_first = _first_build_per_pr(accel_builds)
    first_pass_failures = [b for b in accel_first.values() if b.get("result") != "success"]
    first_pass_successes = [b for b in accel_first.values() if b.get("result") == "success"]

    # Failure type clustering from first-pass failures
    failure_build_ids = {b["build_id"] for b in first_pass_failures}
    failure_type_counter: Counter[str] = Counter()
    failing_tests: Counter[str] = Counter()
    for msg in all_failure_msgs:
        if msg.get("build_id") in failure_build_ids:
            source = msg.get("source", "unknown")
            failure_type_counter[source] += 1
    for tr in all_test_results:
        if tr.get("build_id") in failure_build_ids and tr.get("status") == "failed":
            failing_tests[tr.get("test_name", "unknown")] += 1

    # Infra vs code failures in first-pass
    infra_failures = 0
    code_failures = 0
    for b in first_pass_failures:
        bid = b["build_id"]
        is_infra = any(
            s.get("is_infra") for s in all_builds
            if s.get("build_id") == bid
        )
        if is_infra:
            infra_failures += 1
        else:
            code_failures += 1

    # Component hotspots — multi-attempt vs single-shot
    accel_components: Counter[str] = Counter()
    auto_components: Counter[str] = Counter()
    for issue in accelerated_issues:
        for c in _parse_json_field(issue.get("components")):
            accel_components[c] += 1
    for issue in automated_issues:
        for c in _parse_json_field(issue.get("components")):
            auto_components[c] += 1

    component_hotspots = []
    all_comps = set(accel_components) | set(auto_components)
    for comp in sorted(all_comps):
        a_count = accel_components.get(comp, 0)
        f_count = auto_components.get(comp, 0)
        total = a_count + f_count
        multi_pct = round(a_count / total * 100, 1) if total else 0
        component_hotspots.append({
            "component": comp,
            "multi_attempt": a_count,
            "single_shot": f_count,
            "multi_attempt_pct": multi_pct,
        })
    component_hotspots.sort(key=lambda x: x["multi_attempt"], reverse=True)

    # PR size comparison
    def _pr_size(pr: dict) -> int:
        return (pr.get("additions") or 0) + (pr.get("deletions") or 0)

    accel_sizes = [_pr_size(p) for p in accel_prs]
    auto_sizes = [_pr_size(p) for p in auto_prs]

    # Multi-PR detection — issues with > 1 PR
    multi_pr_issues = [
        {"key": i["key"], "summary": i.get("summary"), "pr_count": len(issue_to_prs.get(i["key"], []))}
        for i in accelerated_issues
        if len(issue_to_prs.get(i["key"], [])) > 1
    ]

    # Comment-based insights for accelerated-fix issues
    comment_themes: Counter[str] = Counter()
    for issue in accelerated_issues:
        blob = _issue_text_blob(issue)
        themes = _scan_text_for_themes(blob)
        comment_themes.update(themes)

    return {
        "available": True,
        "count": len(accelerated_issues),
        "total_prs": len(accel_prs),
        "first_pass_analysis": {
            "total_prs_with_builds": len(accel_first),
            "first_pass_failures": len(first_pass_failures),
            "first_pass_successes": len(first_pass_successes),
            "first_pass_rate": round(
                len(first_pass_successes) / len(accel_first) * 100, 1
            ) if accel_first else 0,
            "infra_failures": infra_failures,
            "code_failures": code_failures,
        },
        "top_failing_tests": [
            {"test": t, "count": c} for t, c in failing_tests.most_common(10)
        ],
        "failure_sources": _counter_to_list(failure_type_counter),
        "component_hotspots": component_hotspots,
        "pr_size_comparison": {
            "accelerated_fix": _percentiles(accel_sizes) if accel_sizes else {},
            "fully_automated": _percentiles(auto_sizes) if auto_sizes else {},
            "accelerated_mean": round(sum(accel_sizes) / len(accel_sizes), 0) if accel_sizes else None,
            "automated_mean": round(sum(auto_sizes) / len(auto_sizes), 0) if auto_sizes else None,
        },
        "multi_pr_issues": multi_pr_issues,
        "themes_from_comments": [
            {"theme": t, "count": c} for t, c in comment_themes.most_common()
        ],
    }


def _compute_ci_impact(
    issue_prs: list[dict],
    all_prs: list[dict],
    all_builds: list[dict],
    all_build_steps: list[dict],
    all_failure_msgs: list[dict],
    period_start: str | None = None,
    period_end: str | None = None,
) -> dict:
    """CI impact of bug bash fix PRs vs same-period baseline."""
    issue_pr_nums = {pr["number"] for pr in issue_prs}

    # Filter builds to those with PR data
    issue_builds = [b for b in all_builds if b.get("pr_number") in issue_pr_nums]
    baseline_pr_nums = {pr["number"] for pr in all_prs} - issue_pr_nums
    baseline_builds = [b for b in all_builds if b.get("pr_number") in baseline_pr_nums]

    def _ci_stats(builds: list[dict], pr_nums: set[int]) -> dict:
        if not builds or not pr_nums:
            return {"available": False}

        by_pr: dict[int, list[dict]] = defaultdict(list)
        for b in builds:
            by_pr[b["pr_number"]].append(b)

        total_builds = len(builds)
        total_prs = len(pr_nums & set(by_pr.keys()))

        # First-pass success rate
        first_pass_ok = 0
        for pr_num in by_pr:
            pr_builds = sorted(by_pr[pr_num], key=lambda b: b.get("started_at") or "")
            if pr_builds and pr_builds[0].get("result") == "success":
                first_pass_ok += 1

        # Retest tax: avg builds per PR
        retest_tax = round(total_builds / total_prs, 2) if total_prs else 0

        # Failure counts
        failures = sum(1 for b in builds if b.get("result") != "success")
        failure_pct = round(failures / total_builds * 100, 1) if total_builds else 0

        # Wasted CI hours (duration of failed builds)
        wasted_seconds = sum(
            b.get("duration_seconds") or 0
            for b in builds if b.get("result") != "success"
        )

        # Infra vs code failure split
        failure_ids = {b["build_id"] for b in builds if b.get("result") != "success"}
        infra_count = 0
        code_count = 0
        for step in all_build_steps:
            if step.get("build_id") in failure_ids and step.get("level") == "Error":
                if step.get("is_infra"):
                    infra_count += 1
                else:
                    code_count += 1

        return {
            "available": True,
            "total_prs": total_prs,
            "total_builds": total_builds,
            "first_pass_success_pct": round(first_pass_ok / total_prs * 100, 1) if total_prs else 0,
            "retest_tax": retest_tax,
            "failure_pct": failure_pct,
            "wasted_ci_hours": round(wasted_seconds / 3600, 1),
            "infra_failures": infra_count,
            "code_failures": code_count,
        }

    return {
        "bug_bash": _ci_stats(issue_builds, issue_pr_nums),
        "baseline": _ci_stats(baseline_builds, baseline_pr_nums),
    }


def _compute_quality_signals(
    issue_prs: list[dict],
    issue_to_prs: dict[str, list[dict]],
    issues: list[dict],
    all_reverts: list[dict],
    risk_scores: list[dict],
    all_prs: list[dict],
) -> dict:
    """Code quality signals for bug bash fix PRs."""
    issue_pr_nums = {pr["number"] for pr in issue_prs}

    # Revert rate
    reverted_pr_nums = set()
    for rev in all_reverts:
        rp = rev.get("reverted_pr")
        if rp and rp in issue_pr_nums:
            reverted_pr_nums.add(rp)
    revert_rate = round(len(reverted_pr_nums) / len(issue_pr_nums) * 100, 1) if issue_pr_nums else 0

    # Regression vs CI signal — did CI catch it before merge?
    regression_issues = [
        i for i in issues
        if "regressions-found" in set(_parse_json_field(i.get("labels")))
    ]

    # PR size distribution: AI-fixed (fully-automated or accelerated) vs all bug bash
    def _pr_size(pr: dict) -> int:
        return (pr.get("additions") or 0) + (pr.get("deletions") or 0)

    ai_success_labels = {"ai-fully-automated", "ai-accelerated-fix"}
    ai_failure_labels = {"ai-could-not-fix", "ai-verification-failed"}
    ai_success_prs = []
    ai_failure_prs = []
    for issue in issues:
        lbls = set(_parse_json_field(issue.get("labels")))
        prs = issue_to_prs.get(issue["key"], [])
        if lbls & ai_success_labels:
            ai_success_prs.extend(prs)
        if lbls & ai_failure_labels:
            ai_failure_prs.extend(prs)

    success_sizes = [_pr_size(p) for p in ai_success_prs]
    failure_sizes = [_pr_size(p) for p in ai_failure_prs]

    # Code risk overlap — do bug bash fixes touch high-risk code?
    risk_by_comp = defaultdict(list)
    for r in risk_scores:
        comp = r.get("component")
        if comp:
            risk_by_comp[comp].append(r)

    touched_components: Counter[str] = Counter()
    for pr in issue_prs:
        for c in _parse_json_field(pr.get("changed_components")):
            touched_components[c] += 1

    high_risk_touches = []
    for comp, count in touched_components.most_common():
        comp_risks = risk_by_comp.get(comp, [])
        if comp_risks:
            avg_risk = sum(r.get("risk_score", 0) for r in comp_risks) / len(comp_risks)
            high_funcs = sum(1 for r in comp_risks if (r.get("risk_score") or 0) >= 7.0)
            if high_funcs > 0 or avg_risk >= 5.0:
                high_risk_touches.append({
                    "component": comp, "pr_touches": count,
                    "avg_risk": round(avg_risk, 2), "high_risk_functions": high_funcs,
                })

    return {
        "revert_rate_pct": revert_rate,
        "reverted_prs": sorted(reverted_pr_nums),
        "regressions_found": len(regression_issues),
        "pr_size_comparison": {
            "ai_success": {
                "count": len(success_sizes),
                "mean": round(sum(success_sizes) / len(success_sizes), 0) if success_sizes else None,
                **_percentiles(success_sizes),
            },
            "ai_failure": {
                "count": len(failure_sizes),
                "mean": round(sum(failure_sizes) / len(failure_sizes), 0) if failure_sizes else None,
                **_percentiles(failure_sizes),
            },
        },
        "high_risk_touches": high_risk_touches,
    }


def _compute_temporal(
    issues: list[dict],
    issue_to_prs: dict[str, list[dict]],
) -> dict:
    """Temporal analysis: daily throughput, time-to-fix, day-of-week patterns."""
    # Daily outcome throughput
    daily_outcomes: dict[str, Counter[str]] = defaultdict(Counter)
    outcome_labels = {
        "ai-fully-automated", "ai-accelerated-fix",
        "ai-could-not-fix", "ai-verification-failed", "regressions-found",
    }

    for issue in issues:
        resolved_dt = _parse_iso(issue.get("resolved"))
        if not resolved_dt:
            continue
        day_str = resolved_dt.strftime("%Y-%m-%d")
        day_name = resolved_dt.strftime("%A")
        issue_labels = set(_parse_json_field(issue.get("labels")))
        for lbl in issue_labels & outcome_labels:
            daily_outcomes[day_str][lbl] += 1
        daily_outcomes[day_str]["_total"] += 1
        daily_outcomes[day_str]["_day_name"] = day_name  # type: ignore[assignment]

    daily_list = []
    for day in sorted(daily_outcomes.keys()):
        counts = daily_outcomes[day]
        day_name = counts.pop("_day_name", "")  # type: ignore[arg-type]
        total = counts.pop("_total", 0)
        daily_list.append({
            "date": day,
            "day_name": day_name,
            "total_outcomes": total,
            "breakdown": dict(counts),
        })

    # Time-to-fix distribution (hours from created to resolved)
    fix_hours: list[dict] = []
    for issue in issues:
        created = _parse_iso(issue.get("created"))
        resolved = _parse_iso(issue.get("resolved"))
        if created and resolved:
            hours = (resolved - created).total_seconds() / 3600
            if hours >= 0:
                issue_labels = set(_parse_json_field(issue.get("labels")))
                outcome = "other"
                for lbl in outcome_labels:
                    if lbl in issue_labels:
                        outcome = lbl
                        break
                fix_hours.append({
                    "key": issue["key"],
                    "hours": round(hours, 1),
                    "outcome": outcome,
                })

    # Group fix times by outcome
    hours_by_outcome: dict[str, list[float]] = defaultdict(list)
    for fh in fix_hours:
        hours_by_outcome[fh["outcome"]].append(fh["hours"])

    fix_time_by_outcome = {}
    for outcome, hours_list in hours_by_outcome.items():
        fix_time_by_outcome[outcome] = {
            "count": len(hours_list),
            "mean": round(sum(hours_list) / len(hours_list), 1) if hours_list else None,
            **_percentiles(hours_list),
        }

    # Day-of-week effectiveness
    dow_counter: Counter[str] = Counter()
    dow_success: Counter[str] = Counter()
    success_labels = {"ai-fully-automated", "ai-accelerated-fix"}
    for issue in issues:
        resolved_dt = _parse_iso(issue.get("resolved"))
        if not resolved_dt:
            continue
        day_name = resolved_dt.strftime("%A")
        dow_counter[day_name] += 1
        if set(_parse_json_field(issue.get("labels"))) & success_labels:
            dow_success[day_name] += 1

    day_effectiveness = []
    for day in ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]:
        total = dow_counter.get(day, 0)
        successes = dow_success.get(day, 0)
        day_effectiveness.append({
            "day": day, "total_resolved": total, "ai_successes": successes,
            "success_rate": round(successes / total * 100, 1) if total else 0,
        })

    return {
        "daily_throughput": daily_list,
        "fix_time_by_outcome": fix_time_by_outcome,
        "day_of_week_effectiveness": day_effectiveness,
    }


def _generate_recommendations(
    nonfixable: dict,
    acceleration_gap: dict,
    ci_impact: dict,
    quality: dict,
    temporal: dict,
) -> list[dict]:
    """Produce threshold-based natural-language findings with severity levels."""
    recs: list[dict] = []

    # --- Nonfixable recommendations ---
    if nonfixable.get("available"):
        # Components with disproportionate nonfixability
        for comp in nonfixable.get("component_contrast", []):
            if comp.get("overrepresented") and comp["nonfixable_count"] >= 2:
                recs.append({
                    "severity": "action",
                    "category": "nonfixable",
                    "finding": (
                        f"{comp['component']}: {comp['nonfixable_count']} nonfixable issues "
                        f"({comp['nonfixable_pct']}% of nonfixable vs {comp['fixable_pct']}% of fixable). "
                        f"Investigate test coverage and AI context for this component."
                    ),
                })

        # Dominant themes
        for theme in nonfixable.get("themes", [])[:3]:
            if theme["count"] >= 2:
                recs.append({
                    "severity": "action" if theme["pct"] >= 25 else "info",
                    "category": "nonfixable",
                    "finding": (
                        f'"{theme["theme"]}" cited in {theme["count"]} nonfixable issues '
                        f'({theme["pct"]}%). '
                        + _theme_recommendation(theme["theme"])
                    ),
                })

        # High-risk code overlap
        for risk in nonfixable.get("code_risk_overlap", []):
            if risk["high_risk_functions"] >= 3:
                recs.append({
                    "severity": "warning",
                    "category": "nonfixable",
                    "finding": (
                        f"{risk['component']}: {risk['high_risk_functions']} high-risk functions "
                        f"(avg risk score {risk['avg_risk_score']}). "
                        f"Nonfixable issues may correlate with code complexity."
                    ),
                })

    # --- Acceleration gap recommendations ---
    if acceleration_gap.get("available"):
        fpa = acceleration_gap.get("first_pass_analysis", {})
        if fpa.get("total_prs_with_builds"):
            fp_rate = fpa.get("first_pass_rate", 100)
            if fp_rate < 50:
                recs.append({
                    "severity": "action",
                    "category": "acceleration_gap",
                    "finding": (
                        f"Only {fp_rate}% of accelerated-fix PRs passed CI on first attempt. "
                        f"{fpa['code_failures']} code failures, {fpa['infra_failures']} infra failures. "
                        f"Improving AI's understanding of test expectations would help."
                    ),
                })
            elif fp_rate < 75:
                recs.append({
                    "severity": "warning",
                    "category": "acceleration_gap",
                    "finding": (
                        f"{fp_rate}% first-pass CI success for accelerated-fix PRs. "
                        f"Room to improve — check top failing tests."
                    ),
                })

        for test in acceleration_gap.get("top_failing_tests", [])[:3]:
            if test["count"] >= 2:
                recs.append({
                    "severity": "action",
                    "category": "acceleration_gap",
                    "finding": (
                        f"Test '{test['test']}' blocked first-attempt success for {test['count']} issues. "
                        f"Adding test expectations/schema docs to AI context would help."
                    ),
                })

        for hs in acceleration_gap.get("component_hotspots", []):
            if hs["multi_attempt"] >= 2 and hs["multi_attempt_pct"] >= 50:
                recs.append({
                    "severity": "warning",
                    "category": "acceleration_gap",
                    "finding": (
                        f"{hs['component']}: {hs['multi_attempt_pct']}% of fixes needed multiple attempts. "
                        f"AI may lack sufficient context for this component."
                    ),
                })

    # --- CI impact recommendations ---
    bb = ci_impact.get("bug_bash", {})
    bl = ci_impact.get("baseline", {})
    if bb.get("available") and bl.get("available"):
        bb_fp = bb.get("first_pass_success_pct", 0)
        bl_fp = bl.get("first_pass_success_pct", 0)
        if bb_fp < bl_fp - 10:
            recs.append({
                "severity": "warning",
                "category": "ci_impact",
                "finding": (
                    f"Bug bash fix PRs: {bb_fp}% first-pass CI success vs {bl_fp}% baseline. "
                    f"AI-generated code triggers more test failures than human code."
                ),
            })

        if bb.get("retest_tax", 0) > bl.get("retest_tax", 0) * 1.3:
            recs.append({
                "severity": "info",
                "category": "ci_impact",
                "finding": (
                    f"Bug bash retest tax: {bb['retest_tax']} builds/PR vs {bl['retest_tax']} baseline. "
                    f"More CI retries needed for AI-generated fixes."
                ),
            })

        if bb.get("wasted_ci_hours", 0) > 10:
            recs.append({
                "severity": "info",
                "category": "ci_impact",
                "finding": (
                    f"{bb['wasted_ci_hours']:.1f} CI hours wasted on failed bug bash builds. "
                    f"Pre-submit validation could reduce this."
                ),
            })

    # --- Quality signal recommendations ---
    if quality.get("revert_rate_pct", 0) > 5:
        recs.append({
            "severity": "action",
            "category": "quality",
            "finding": (
                f"{quality['revert_rate_pct']}% of bug bash fix PRs were reverted. "
                f"Consider requiring human review for AI-generated fixes in high-risk areas."
            ),
        })

    if quality.get("regressions_found", 0) > 0:
        recs.append({
            "severity": "warning",
            "category": "quality",
            "finding": (
                f"{quality['regressions_found']} regressions found during bug bash. "
                f"Verify that CI caught these before merge."
            ),
        })

    for risk in quality.get("high_risk_touches", []):
        if risk["high_risk_functions"] >= 5:
            recs.append({
                "severity": "warning",
                "category": "quality",
                "finding": (
                    f"Bug bash fixes touched {risk['high_risk_functions']} high-risk functions in "
                    f"{risk['component']}. These hotspots need extra review even for AI fixes."
                ),
            })

    # --- Temporal recommendations ---
    day_eff = temporal.get("day_of_week_effectiveness", [])
    if day_eff:
        best_day = max(day_eff, key=lambda d: d["total_resolved"])
        total_all = sum(d["total_resolved"] for d in day_eff)
        if total_all > 0 and best_day["total_resolved"] / total_all >= 0.3:
            recs.append({
                "severity": "info",
                "category": "temporal",
                "finding": (
                    f"{best_day['day']} produced {best_day['total_resolved']} outcomes "
                    f"({round(best_day['total_resolved'] / total_all * 100)}% of total). "
                    f"Deep-work policies on this day were effective."
                ),
            })

    # Sort by severity
    severity_order = {"action": 0, "warning": 1, "info": 2}
    recs.sort(key=lambda r: severity_order.get(r["severity"], 9))

    return recs


def _theme_recommendation(theme: str) -> str:
    """Return a concrete recommendation for a nonfixability theme."""
    mapping = {
        "no test coverage": (
            "Adding e2e or unit tests for these areas would make them AI-fixable."
        ),
        "multi-service / cross-repo": (
            "Consider repo-spanning AI context or explicitly scope these out of AI triage."
        ),
        "UI / visual": (
            "Visual regression tooling (screenshot comparison) would enable AI verification."
        ),
        "flaky / non-deterministic": (
            "Stabilize flaky tests first — AI cannot fix what it cannot reproduce."
        ),
        "insufficient context": (
            "Improve bug report templates to include reproduction steps and expected behavior."
        ),
        "infrastructure / environment": (
            "These require cluster access. Consider environment-in-a-box for AI tooling."
        ),
        "complex state machine": (
            "Break down complex logic into smaller, testable units with clear contracts."
        ),
    }
    return mapping.get(theme, "Review these issues for common patterns.")


def compute_bug_bash_intelligence(
    issues: list[dict],
    store: Store,
    collection_cfg: dict | None = None,
) -> dict:
    """Cross-reference bug bash JIRA issues with PRs, CI, reverts, and code risk.

    This is the main intelligence entry point — it calls the JIRA-only analyzer
    plus all cross-referenced analysis functions.
    """
    if not issues:
        return {"available": False}

    all_prs = store.get_merged_prs()
    all_builds = store.get_ci_builds()
    all_test_results = store.get_all_test_results()
    all_failure_msgs = store.get_all_build_failure_messages()
    all_build_steps = store.get_all_build_steps()
    all_reverts = store.get_reverts()
    risk_scores = store.get_code_risk_scores()

    # Join issues to PRs
    issue_to_prs = _join_issues_to_prs(issues, all_prs)
    issue_prs = []
    for prs in issue_to_prs.values():
        issue_prs.extend(prs)
    # Deduplicate PRs by number
    seen = set()
    unique_issue_prs = []
    for pr in issue_prs:
        if pr["number"] not in seen:
            seen.add(pr["number"])
            unique_issue_prs.append(pr)

    # Classify issues by label
    fixable_issues = []
    nonfixable_issues = []
    automated_issues = []
    accelerated_issues = []
    for issue in issues:
        lbls = set(_parse_json_field(issue.get("labels")))
        if "ai-fixable" in lbls:
            fixable_issues.append(issue)
        if "ai-nonfixable" in lbls:
            nonfixable_issues.append(issue)
        if "ai-fully-automated" in lbls:
            automated_issues.append(issue)
        if "ai-accelerated-fix" in lbls:
            accelerated_issues.append(issue)

    log.info(
        "Bug bash intelligence: %d issues, %d linked PRs, "
        "%d nonfixable, %d accelerated, %d fully-automated",
        len(issues), len(unique_issue_prs),
        len(nonfixable_issues), len(accelerated_issues), len(automated_issues),
    )

    nonfixable = _analyze_nonfixable(nonfixable_issues, fixable_issues, risk_scores)
    acceleration_gap = _analyze_acceleration_gap(
        accelerated_issues, automated_issues, issue_to_prs,
        all_builds, all_test_results, all_failure_msgs, risk_scores,
    )
    ci_impact = _compute_ci_impact(
        unique_issue_prs, all_prs, all_builds, all_build_steps, all_failure_msgs,
    )
    quality = _compute_quality_signals(
        unique_issue_prs, issue_to_prs, issues, all_reverts, risk_scores, all_prs,
    )
    temporal = _compute_temporal(issues, issue_to_prs)
    recommendations = _generate_recommendations(
        nonfixable, acceleration_gap, ci_impact, quality, temporal,
    )

    return {
        "available": True,
        "linked_prs": len(unique_issue_prs),
        "nonfixable_analysis": nonfixable,
        "acceleration_gap": acceleration_gap,
        "ci_impact": ci_impact,
        "quality_signals": quality,
        "temporal": temporal,
        "recommendations": recommendations,
    }
