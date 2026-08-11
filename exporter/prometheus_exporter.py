"""Expose engineering metrics as a Prometheus /metrics endpoint + JSON table API."""

from __future__ import annotations

import json
import logging
import time
import threading
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

from prometheus_client import Gauge, generate_latest, CONTENT_TYPE_LATEST

from collections import Counter, defaultdict

from metrics.calculator import compute_all
from metrics import ci_efficiency
from reports.failure_patterns import _detect_manifest_regressions, _detect_regression_onset
from store.db import Store

log = logging.getLogger(__name__)

# --- Aggregate metrics (no labels) ---

DF_RELEASE_COUNT = Gauge("odh_eng_deployment_frequency_releases_total", "Total upstream releases")
DF_RELEASE_GAP_DAYS = Gauge("odh_eng_deployment_frequency_release_gap_days", "Avg days between releases")
DF_PR_COUNT = Gauge("odh_eng_deployment_frequency_pr_merges_total", "Total merged PRs to main")
DF_PR_GAP_DAYS = Gauge("odh_eng_deployment_frequency_pr_gap_days", "Avg days between PR merges")

LT_CYCLE_P50 = Gauge("odh_eng_lead_time_pr_cycle_p50_hours", "PR cycle time p50 (hours)")
LT_CYCLE_P90 = Gauge("odh_eng_lead_time_pr_cycle_p90_hours", "PR cycle time p90 (hours)")
LT_REVIEW_P50 = Gauge("odh_eng_lead_time_pr_review_p50_hours", "PR review time p50 (hours)")
LT_REVIEW_P90 = Gauge("odh_eng_lead_time_pr_review_p90_hours", "PR review time p90 (hours)")
LT_TO_RELEASE_P50 = Gauge("odh_eng_lead_time_to_release_p50_hours", "Merge to release p50 (hours)")
LT_TO_RELEASE_P90 = Gauge("odh_eng_lead_time_to_release_p90_hours", "Merge to release p90 (hours)")

CFR_RATE = Gauge("odh_eng_change_failure_rate", "Change failure rate (0-1)")
CFR_PATCH_RELEASES = Gauge("odh_eng_change_failure_patch_releases", "Number of patch releases")
CFR_REVERTS = Gauge("odh_eng_change_failure_reverts", "Number of reverts on main")
CFR_CHERRY_PICKS = Gauge("odh_eng_change_failure_cherry_picks", "Human cherry-picks to frozen branches")

MTTR_PATCH_P50 = Gauge("odh_eng_mttr_patch_turnaround_p50_hours", "Patch release turnaround p50 (hours)")
MTTR_PATCH_P90 = Gauge("odh_eng_mttr_patch_turnaround_p90_hours", "Patch release turnaround p90 (hours)")

# --- Per-release metrics (labeled by release) ---

REL_PR_COUNT = Gauge("odh_eng_release_pr_count", "PRs in this release", ["release"])
REL_DAYS_SINCE_PREV = Gauge("odh_eng_release_days_since_previous", "Days since previous release", ["release"])
REL_LEAD_P50 = Gauge("odh_eng_release_lead_time_p50_hours", "Merge-to-release lead time p50", ["release"])
REL_LEAD_P90 = Gauge("odh_eng_release_lead_time_p90_hours", "Merge-to-release lead time p90", ["release"])
REL_CYCLE_P50 = Gauge("odh_eng_release_cycle_time_p50_hours", "PR cycle time p50", ["release"])
REL_CYCLE_P90 = Gauge("odh_eng_release_cycle_time_p90_hours", "PR cycle time p90", ["release"])
REL_CHERRY_PICKS = Gauge("odh_eng_release_cherry_picks", "Cherry-picks on downstream branch", ["release"])
REL_HAS_PATCH = Gauge("odh_eng_release_has_patch", "Release needed a patch (1/0)", ["release"])
REL_PATCH_HOURS = Gauge("odh_eng_release_patch_turnaround_hours", "Hours from .0 to first patch", ["release"])

_PER_RELEASE_GAUGES = [
    REL_PR_COUNT, REL_DAYS_SINCE_PREV,
    REL_LEAD_P50, REL_LEAD_P90,
    REL_CYCLE_P50, REL_CYCLE_P90,
    REL_CHERRY_PICKS, REL_HAS_PATCH, REL_PATCH_HOURS,
]

# --- Throughput over time (labeled by month) ---

MONTHLY_PRS = Gauge("odh_eng_monthly_prs_merged", "PRs merged per month", ["month"])
MONTHLY_RELEASES = Gauge("odh_eng_monthly_releases", "Releases per month", ["month", "type"])
MONTHLY_CHERRY_PICKS = Gauge("odh_eng_monthly_cherry_picks", "Cherry-picks per month", ["month"])
MONTHLY_REVERTS = Gauge("odh_eng_monthly_reverts", "Reverts per month", ["month"])

_MONTHLY_GAUGES = [MONTHLY_PRS, MONTHLY_RELEASES, MONTHLY_CHERRY_PICKS, MONTHLY_REVERTS]

# --- Failure analysis (labeled by branch / month) ---

FAIL_CP_BY_BRANCH = Gauge("odh_eng_failure_cherry_picks_by_branch", "Cherry-picks per branch", ["branch"])
FAIL_CP_MONTHLY = Gauge("odh_eng_failure_cherry_picks_monthly", "Cherry-picks per month", ["month"])
FAIL_REVERTS_MONTHLY = Gauge("odh_eng_failure_reverts_monthly", "Reverts per month", ["month"])

_FAILURE_GAUGES = [FAIL_CP_BY_BRANCH, FAIL_CP_MONTHLY, FAIL_REVERTS_MONTHLY]

# --- PR flow (labeled by bucket) ---

PR_TTR_BUCKET = Gauge("odh_eng_pr_time_to_release_bucket", "PRs per time-to-release bucket", ["bucket"])
PR_CYCLE_BUCKET = Gauge("odh_eng_pr_cycle_time_bucket", "PRs per cycle time bucket", ["bucket"])

_FLOW_GAUGES = [PR_TTR_BUCKET, PR_CYCLE_BUCKET]

# --- Pipeline velocity (labeled by release) ---

PIPE_ACCUM = Gauge("odh_eng_pipeline_accumulation_days", "Days from first PR merge to release tag", ["release"])
PIPE_DOWNSTREAM = Gauge("odh_eng_pipeline_downstream_days", "Days from tag to downstream branch", ["release"])

_PIPELINE_GAUGES = [PIPE_ACCUM, PIPE_DOWNSTREAM]

# --- AI adoption ---
# Windowed aggregates use a simple "window" label for Grafana dropdown filtering.
# Monthly breakdowns use a "window" label so panels filter by the same dropdown.

WINDOWS = {
    "last_month": 2,
    "last_3_months": 3,
    "last_6_months": 6,
    "last_year": 12,
    "all_time": 9999,
}

AI_WIN_TOTAL = Gauge("odh_eng_ai_window_total", "AI-assisted commits in window", ["window"])
AI_WIN_NON_AI = Gauge("odh_eng_ai_window_non_ai", "Non-AI-labeled commits in window", ["window"])
AI_WIN_PCT = Gauge("odh_eng_ai_window_pct", "AI-assisted % in window", ["window"])
AI_WIN_TOOL = Gauge("odh_eng_ai_window_by_tool", "AI-assisted commits by tool in window", ["window", "tool"])
AI_MONTHLY = Gauge("odh_eng_ai_monthly_commits", "AI-assisted commits per month", ["month", "window"])
AI_MONTHLY_PCT = Gauge("odh_eng_ai_monthly_pct", "AI-assisted % of PRs per month", ["month", "window"])
AI_MONTHLY_TOOL = Gauge("odh_eng_ai_monthly_by_tool", "AI-assisted commits per tool per month", ["month", "tool", "window"])

_AI_GAUGES = [AI_WIN_TOTAL, AI_WIN_NON_AI, AI_WIN_PCT, AI_WIN_TOOL, AI_MONTHLY, AI_MONTHLY_PCT, AI_MONTHLY_TOOL]

# --- CI Efficiency ---
# Metrics report at the *test cycle* level: one cycle = one push/retest that
# triggers all CI jobs for a PR in parallel.
# Summary gauges carry a "period" label for time-scoped dashboarding.

CI_PERIODS = {
    "1w": 7,
    "3w": 21,
    "1m": 30,
    "3m": 90,
    "6m": 180,
    "all": None,
}

CI_FIRST_PASS_RATE = Gauge("odh_eng_ci_first_pass_success_rate", "First-cycle CI success rate (0-1)", ["period"])
CI_RETEST_TAX = Gauge("odh_eng_ci_retest_tax", "Average test cycles per PR (ideal = 1.0)", ["period"])
CI_CYCLE_FAILURE_RATE = Gauge("odh_eng_ci_cycle_failure_rate", "CI cycle failure rate (0-1)", ["period"])
CI_TOTAL_CYCLES = Gauge("odh_eng_ci_total_cycles", "Total CI test cycles", ["period"])
CI_TOTAL_JOB_RUNS = Gauge("odh_eng_ci_total_job_runs", "Total individual CI job runs", ["period"])
CI_TOTAL_PRS = Gauge("odh_eng_ci_total_prs_with_ci", "PRs with CI build data", ["period"])
CI_DURATION_P50 = Gauge("odh_eng_ci_cycle_duration_p50_minutes", "Cycle duration p50 (minutes, wall-clock)", ["period"])
CI_DURATION_P90 = Gauge("odh_eng_ci_cycle_duration_p90_minutes", "Cycle duration p90 (minutes, wall-clock)", ["period"])
CI_HOURS_PER_PR_P50 = Gauge("odh_eng_ci_hours_per_pr_p50", "CI wait hours per PR p50", ["period"])
CI_HOURS_PER_PR_P90 = Gauge("odh_eng_ci_hours_per_pr_p90", "CI wait hours per PR p90", ["period"])

CI_MONTHLY_CYCLES = Gauge("odh_eng_ci_monthly_cycles", "CI test cycles per month", ["month"])
CI_MONTHLY_FAILURES = Gauge("odh_eng_ci_monthly_failures", "CI cycle failures per month", ["month"])
CI_MONTHLY_FAILURE_PCT = Gauge("odh_eng_ci_monthly_failure_pct", "CI cycle failure % per month", ["month"])
CI_MONTHLY_RETEST = Gauge("odh_eng_ci_monthly_retest_tax", "Retest tax per month", ["month"])

CI_WEEKLY_CYCLES = Gauge("odh_eng_ci_weekly_cycles", "CI test cycles per week", ["week"])
CI_WEEKLY_CYCLE_FAILURES = Gauge("odh_eng_ci_weekly_cycle_failures", "CI cycle failures per week", ["week"])
CI_WEEKLY_FAILURE_PCT = Gauge("odh_eng_ci_weekly_failure_pct", "CI cycle failure % per week", ["week"])
CI_WEEKLY_JOB_FAILURES = Gauge("odh_eng_ci_weekly_job_failures", "CI job failures per week by test type", ["week", "job"])

_CI_GAUGES = [
    CI_MONTHLY_CYCLES, CI_MONTHLY_FAILURES, CI_MONTHLY_FAILURE_PCT, CI_MONTHLY_RETEST,
    CI_WEEKLY_CYCLES, CI_WEEKLY_CYCLE_FAILURES, CI_WEEKLY_FAILURE_PCT, CI_WEEKLY_JOB_FAILURES,
]

# --- Git-CI Engineering Intelligence ---

COMP_CI_FAILURE_RATE = Gauge("odh_eng_component_ci_failure_rate", "CI failure rate by component", ["component"])
COMP_CI_RETEST_TAX = Gauge("odh_eng_component_ci_retest_tax", "Retest tax by component", ["component"])
COMP_CI_PR_COUNT = Gauge("odh_eng_component_ci_pr_count", "PRs touching component", ["component"])
COMP_CI_AVG_DURATION = Gauge("odh_eng_component_ci_avg_duration_min", "Avg CI duration (minutes) by component", ["component"])

CODE_CRITICAL_FUNCS = Gauge("odh_eng_code_critical_functions", "Critical-risk functions by component", ["component"])
CODE_HIGH_FUNCS = Gauge("odh_eng_code_high_risk_functions", "High-risk functions by component", ["component"])
CODE_AVG_RISK = Gauge("odh_eng_code_avg_risk_score", "Avg risk score by component", ["component"])
CODE_RISK_CI_FAILURE = Gauge("odh_eng_code_risk_ci_failure_rate", "CI failure rate by risk band", ["risk_band"])

COMP_CI_CPU_HOURS = Gauge("odh_eng_component_ci_cpu_hours", "CPU-hours consumed by component", ["component"])
COMP_CI_MEM_GB_HOURS = Gauge("odh_eng_component_ci_memory_gb_hours", "Memory GB-hours by component", ["component"])

AI_CI_PR_COUNT = Gauge("odh_eng_ai_ci_pr_count", "Number of AI-assisted PRs with CI data")
AI_CI_FAILURE_RATE = Gauge("odh_eng_ai_ci_failure_rate", "AI-assisted PR CI failure rate")
AI_CI_RETEST_TAX = Gauge("odh_eng_ai_ci_retest_tax", "AI-assisted PR retest tax")
AI_CI_FIRST_PASS = Gauge("odh_eng_ai_ci_first_pass_rate", "AI-assisted PR first-pass success rate")
AI_CI_PCT_CYCLES = Gauge("odh_eng_ai_ci_pct_of_cycles", "% of CI cycles from AI-assisted PRs")

JIRA_CI_FAILURE_RATE = Gauge("odh_eng_jira_ci_failure_rate", "CI failure rate per Jira ticket", ["jira_key"])
JIRA_CI_RETEST_TAX = Gauge("odh_eng_jira_ci_retest_tax", "Retest tax per Jira ticket", ["jira_key"])
JIRA_CI_CYCLES = Gauge("odh_eng_jira_ci_cycles", "CI cycles per Jira ticket", ["jira_key"])
JIRA_CI_PR_COUNT = Gauge("odh_eng_jira_ci_pr_count", "PRs per Jira ticket", ["jira_key"])

JIRA_TYPE_CI_FAILURE_RATE = Gauge("odh_eng_jira_type_ci_failure_rate", "CI failure rate by issue type", ["issue_type"])
JIRA_TYPE_CI_RETEST_TAX = Gauge("odh_eng_jira_type_ci_retest_tax", "Retest tax by issue type", ["issue_type"])
JIRA_PRIORITY_CI_FAILURE_RATE = Gauge("odh_eng_jira_priority_ci_failure_rate", "CI failure rate by priority", ["priority"])
JIRA_PRIORITY_CI_RETEST_TAX = Gauge("odh_eng_jira_priority_ci_retest_tax", "Retest tax by priority", ["priority"])

REL_CI_FAILURE_RATE = Gauge("odh_eng_release_ci_failure_rate", "CI failure rate per release", ["release"])
REL_CI_RETEST_TAX = Gauge("odh_eng_release_ci_retest_tax", "Retest tax per release", ["release"])
REL_CI_TOTAL_CYCLES = Gauge("odh_eng_release_ci_total_cycles", "Total CI cycles per release", ["release"])

REVERT_TOTAL_WITH_CI = Gauge("odh_eng_revert_total_with_ci", "Reverts where original PR had CI data")
REVERT_CI_WARNED_PCT = Gauge("odh_eng_revert_ci_warned_pct", "% of reverts where CI had failures")

# --- Step-level enrichment ---

COMP_STEP_FAILURES = Gauge("odh_eng_component_step_failures",
    "Step failures per component", ["component", "step"])
COMP_DURATION_BREAKDOWN = Gauge("odh_eng_component_duration_breakdown_minutes",
    "Avg duration breakdown per component", ["component", "category"])
COMP_INFRA_FAILURE_PCT = Gauge("odh_eng_component_infra_failure_pct",
    "% of failures attributed to infrastructure per component", ["component"])
COMP_CODE_FAILURE_PCT = Gauge("odh_eng_component_code_failure_pct",
    "% of failures attributed to code per component", ["component"])

# --- Test stability & manifest regressions ---

TEST_STABILITY_BROKEN = Gauge("odh_eng_test_stability_broken_count", "Number of broken tests (>80% fail rate)", ["period"])
TEST_STABILITY_FLAKY = Gauge("odh_eng_test_stability_flaky_count", "Number of flaky tests (20-80% fail rate)", ["period"])
MANIFEST_REGRESSIONS_TOTAL = Gauge("odh_eng_manifest_regressions_total", "Detected manifest-induced CI regressions", ["period"])

_STABILITY_GAUGES = [TEST_STABILITY_BROKEN, TEST_STABILITY_FLAKY, MANIFEST_REGRESSIONS_TOTAL]

_GIT_CI_LABELED_GAUGES = [
    COMP_CI_FAILURE_RATE, COMP_CI_RETEST_TAX, COMP_CI_PR_COUNT, COMP_CI_AVG_DURATION,
    CODE_CRITICAL_FUNCS, CODE_HIGH_FUNCS, CODE_AVG_RISK, CODE_RISK_CI_FAILURE,
    COMP_CI_CPU_HOURS, COMP_CI_MEM_GB_HOURS,
    JIRA_CI_FAILURE_RATE, JIRA_CI_RETEST_TAX, JIRA_CI_CYCLES, JIRA_CI_PR_COUNT,
    JIRA_TYPE_CI_FAILURE_RATE, JIRA_TYPE_CI_RETEST_TAX,
    JIRA_PRIORITY_CI_FAILURE_RATE, JIRA_PRIORITY_CI_RETEST_TAX,
    REL_CI_FAILURE_RATE, REL_CI_RETEST_TAX, REL_CI_TOTAL_CYCLES,
    COMP_STEP_FAILURES, COMP_DURATION_BREAKDOWN,
    COMP_INFRA_FAILURE_PCT, COMP_CODE_FAILURE_PCT,
]


def _update_metrics(result: dict) -> None:
    df = result["deployment_frequency"]
    DF_RELEASE_COUNT.set(df["releases"]["total"])
    if df["releases"]["avg_gap_days"] is not None:
        DF_RELEASE_GAP_DAYS.set(df["releases"]["avg_gap_days"])
    DF_PR_COUNT.set(df["pr_merges"]["total"])
    if df["pr_merges"]["avg_gap_days"] is not None:
        DF_PR_GAP_DAYS.set(df["pr_merges"]["avg_gap_days"])

    lt = result["lead_time"]
    for key, p50_gauge, p90_gauge in [
        ("pr_cycle_time_hours", LT_CYCLE_P50, LT_CYCLE_P90),
        ("pr_review_time_hours", LT_REVIEW_P50, LT_REVIEW_P90),
        ("to_release_hours", LT_TO_RELEASE_P50, LT_TO_RELEASE_P90),
    ]:
        data = lt.get(key, {})
        if data.get("p50") is not None:
            p50_gauge.set(data["p50"])
        if data.get("p90") is not None:
            p90_gauge.set(data["p90"])

    cfr = result["change_failure_rate"]
    if cfr["rate"] is not None:
        CFR_RATE.set(cfr["rate"])
    CFR_PATCH_RELEASES.set(cfr["patch_releases"])
    CFR_REVERTS.set(cfr["reverts_on_main"])
    CFR_CHERRY_PICKS.set(cfr["human_cherry_picks"])

    mt = result["mttr"]
    ptr = mt["patch_release_turnaround_hours"]
    if ptr.get("p50") is not None:
        MTTR_PATCH_P50.set(ptr["p50"])
    if ptr.get("p90") is not None:
        MTTR_PATCH_P90.set(ptr["p90"])

    _update_per_release(result.get("per_release", []))
    _update_throughput(result.get("throughput", {}))
    _update_failure_analysis(result.get("failure_analysis", {}))
    _update_pr_flow(result.get("pr_flow", {}))
    _update_pipeline_velocity(result.get("pipeline_velocity", []))
    _update_ai_adoption(result.get("ai_adoption", {}))
    _update_ci_efficiency(result.get("ci_efficiency", {}))
    _update_git_ci_insights(result.get("git_ci_insights", {}))


def _update_per_release(releases: list[dict]) -> None:
    for gauge in _PER_RELEASE_GAUGES:
        gauge._metrics.clear()

    for rel in releases:
        label = rel["label"]
        REL_PR_COUNT.labels(release=label).set(rel["pr_count"])
        REL_CHERRY_PICKS.labels(release=label).set(rel["cherry_picks"])
        REL_HAS_PATCH.labels(release=label).set(1 if rel["has_patch"] else 0)
        if rel["days_since_previous"] is not None:
            REL_DAYS_SINCE_PREV.labels(release=label).set(rel["days_since_previous"])
        if rel["lead_time_p50"] is not None:
            REL_LEAD_P50.labels(release=label).set(rel["lead_time_p50"])
        if rel["lead_time_p90"] is not None:
            REL_LEAD_P90.labels(release=label).set(rel["lead_time_p90"])
        if rel["cycle_time_p50"] is not None:
            REL_CYCLE_P50.labels(release=label).set(rel["cycle_time_p50"])
        if rel["cycle_time_p90"] is not None:
            REL_CYCLE_P90.labels(release=label).set(rel["cycle_time_p90"])
        if rel["patch_turnaround_hours"] is not None:
            REL_PATCH_HOURS.labels(release=label).set(rel["patch_turnaround_hours"])


def _update_throughput(data: dict) -> None:
    for gauge in _MONTHLY_GAUGES:
        gauge._metrics.clear()

    for m in data.get("months", []):
        month = m["month"]
        MONTHLY_PRS.labels(month=month).set(m["prs_merged"])
        MONTHLY_RELEASES.labels(month=month, type="stable").set(m["releases_stable"])
        MONTHLY_RELEASES.labels(month=month, type="ea").set(m["releases_ea"])
        MONTHLY_RELEASES.labels(month=month, type="patch").set(m["releases_patch"])
        MONTHLY_CHERRY_PICKS.labels(month=month).set(m["cherry_picks"])
        MONTHLY_REVERTS.labels(month=month).set(m["reverts"])


def _update_failure_analysis(data: dict) -> None:
    for gauge in _FAILURE_GAUGES:
        gauge._metrics.clear()

    for entry in data.get("cherry_picks_by_branch", []):
        FAIL_CP_BY_BRANCH.labels(branch=entry["branch"]).set(entry["count"])

    for entry in data.get("monthly_failures", []):
        month = entry["month"]
        FAIL_CP_MONTHLY.labels(month=month).set(entry["cherry_picks"])
        FAIL_REVERTS_MONTHLY.labels(month=month).set(entry["reverts"])


def _update_pr_flow(data: dict) -> None:
    for gauge in _FLOW_GAUGES:
        gauge._metrics.clear()

    for entry in data.get("time_to_release", []):
        PR_TTR_BUCKET.labels(bucket=entry["bucket"]).set(entry["count"])

    for entry in data.get("cycle_time", []):
        PR_CYCLE_BUCKET.labels(bucket=entry["bucket"]).set(entry["count"])


def _update_pipeline_velocity(releases: list[dict]) -> None:
    for gauge in _PIPELINE_GAUGES:
        gauge._metrics.clear()

    for rel in releases:
        label = rel["label"]
        if rel["accumulation_days"] is not None:
            PIPE_ACCUM.labels(release=label).set(rel["accumulation_days"])
        if rel["downstream_days"] is not None:
            PIPE_DOWNSTREAM.labels(release=label).set(rel["downstream_days"])


def _month_age(month_str: str) -> int:
    """Return how many months ago this month is (0=current)."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    current = now.year * 12 + now.month
    parts = month_str.split("-")
    data = int(parts[0]) * 12 + int(parts[1])
    return max(0, current - data)


def _update_ai_adoption(data: dict) -> None:
    for gauge in _AI_GAUGES:
        gauge._metrics.clear()

    months = data.get("months", [])

    for window_name, max_age in WINDOWS.items():
        filtered = [m for m in months if _month_age(m["month"]) < max_age]

        ai_total = sum(m["ai_commits"] for m in filtered)
        pr_total = sum(m["total_prs"] for m in filtered)
        pct = round(ai_total / pr_total * 100, 1) if pr_total > 0 else 0

        AI_WIN_TOTAL.labels(window=window_name).set(ai_total)
        AI_WIN_NON_AI.labels(window=window_name).set(max(0, pr_total - ai_total))
        AI_WIN_PCT.labels(window=window_name).set(pct)

        tool_totals: dict[str, int] = {}
        for m in filtered:
            for tool, count in m.get("by_tool", {}).items():
                tool_totals[tool] = tool_totals.get(tool, 0) + count
        for tool, count in tool_totals.items():
            AI_WIN_TOOL.labels(window=window_name, tool=tool).set(count)

        for m in filtered:
            month = m["month"]
            AI_MONTHLY.labels(month=month, window=window_name).set(m["ai_commits"])
            AI_MONTHLY_PCT.labels(month=month, window=window_name).set(m["ai_pct"])
            for tool, count in m.get("by_tool", {}).items():
                AI_MONTHLY_TOOL.labels(month=month, tool=tool, window=window_name).set(count)


def _set_ci_summary_gauges(summary: dict, period: str) -> None:
    """Set CI summary gauges for a single period."""
    if summary.get("first_pass_success_rate") is not None:
        CI_FIRST_PASS_RATE.labels(period=period).set(summary["first_pass_success_rate"])
    if summary.get("retest_tax") is not None:
        CI_RETEST_TAX.labels(period=period).set(summary["retest_tax"])
    if summary.get("cycle_failure_rate") is not None:
        CI_CYCLE_FAILURE_RATE.labels(period=period).set(summary["cycle_failure_rate"])
    CI_TOTAL_CYCLES.labels(period=period).set(summary.get("total_cycles", 0))
    CI_TOTAL_JOB_RUNS.labels(period=period).set(summary.get("total_job_runs", 0))
    CI_TOTAL_PRS.labels(period=period).set(summary.get("total_prs_with_ci", 0))

    dur = summary.get("cycle_duration_minutes", {})
    if dur.get("p50") is not None:
        CI_DURATION_P50.labels(period=period).set(dur["p50"])
    if dur.get("p90") is not None:
        CI_DURATION_P90.labels(period=period).set(dur["p90"])

    ci_hrs = summary.get("ci_hours_per_pr", {})
    if ci_hrs.get("p50") is not None:
        CI_HOURS_PER_PR_P50.labels(period=period).set(ci_hrs["p50"])
    if ci_hrs.get("p90") is not None:
        CI_HOURS_PER_PR_P90.labels(period=period).set(ci_hrs["p90"])


def _update_ci_efficiency(data: dict) -> None:
    if not data.get("available"):
        return

    for gauge in _CI_GAUGES:
        gauge._metrics.clear()

    all_builds = data.get("builds", [])
    now = datetime.now(timezone.utc)

    for period_name, days in CI_PERIODS.items():
        if days is not None:
            cutoff = (now - timedelta(days=days)).isoformat()
            filtered = [
                b for b in all_builds
                if b.get("started_at") and b["started_at"] >= cutoff
            ]
        else:
            filtered = all_builds

        summary = ci_efficiency.compute_summary(filtered)
        _set_ci_summary_gauges(summary, period_name)

    for m in data.get("monthly", []):
        month = m["month"]
        CI_MONTHLY_CYCLES.labels(month=month).set(m["cycles"])
        CI_MONTHLY_FAILURES.labels(month=month).set(m["failures"])
        CI_MONTHLY_FAILURE_PCT.labels(month=month).set(m["failure_pct"])
        CI_MONTHLY_RETEST.labels(month=month).set(m["retest_tax"])

    for w in data.get("weekly_failures", []):
        week = w["week"]
        total = w["total"]
        failures = w["failures"]
        CI_WEEKLY_CYCLES.labels(week=week).set(total)
        CI_WEEKLY_CYCLE_FAILURES.labels(week=week).set(failures)
        pct = round(failures / total * 100, 1) if total > 0 else 0
        CI_WEEKLY_FAILURE_PCT.labels(week=week).set(pct)

    for wj in data.get("weekly_job_failures", []):
        CI_WEEKLY_JOB_FAILURES.labels(week=wj["week"], job=wj["job"]).set(wj["failures"])


def _update_git_ci_insights(data: dict) -> None:
    if not data:
        return

    for gauge in _GIT_CI_LABELED_GAUGES:
        gauge._metrics.clear()

    for entry in data.get("component_health", []):
        comp = entry["component"]
        if entry.get("cycle_failure_rate") is not None:
            COMP_CI_FAILURE_RATE.labels(component=comp).set(entry["cycle_failure_rate"])
        if entry.get("retest_tax") is not None:
            COMP_CI_RETEST_TAX.labels(component=comp).set(entry["retest_tax"])
        COMP_CI_PR_COUNT.labels(component=comp).set(entry.get("total_prs_with_ci", 0))
        dur = entry.get("cycle_duration_minutes", {})
        if dur.get("mean") is not None:
            COMP_CI_AVG_DURATION.labels(component=comp).set(dur["mean"])

    hotspots = data.get("code_hotspots", {})
    if hotspots.get("available"):
        for entry in hotspots.get("by_risk_band", []):
            band = entry["risk_band"]
            if entry.get("failure_rate") is not None:
                CODE_RISK_CI_FAILURE.labels(risk_band=band).set(entry["failure_rate"])

    risk_summary = data.get("_risk_summary", [])
    for entry in risk_summary:
        comp = entry["component"]
        CODE_CRITICAL_FUNCS.labels(component=comp).set(entry.get("critical", 0))
        CODE_HIGH_FUNCS.labels(component=comp).set(entry.get("high", 0))
        if entry.get("avg_risk") is not None:
            CODE_AVG_RISK.labels(component=comp).set(entry["avg_risk"])

    for entry in data.get("component_resource_cost", []):
        comp = entry["component"]
        COMP_CI_CPU_HOURS.labels(component=comp).set(entry.get("cpu_hours", 0))
        COMP_CI_MEM_GB_HOURS.labels(component=comp).set(entry.get("memory_gb_hours", 0))

    ai = data.get("ai_summary", {})
    if ai.get("available"):
        AI_CI_PR_COUNT.set(ai.get("ai_pr_count", 0))
        if ai.get("failure_rate") is not None:
            AI_CI_FAILURE_RATE.set(ai["failure_rate"])
        if ai.get("retest_tax") is not None:
            AI_CI_RETEST_TAX.set(ai["retest_tax"])
        if ai.get("first_pass_rate") is not None:
            AI_CI_FIRST_PASS.set(ai["first_pass_rate"])
        AI_CI_PCT_CYCLES.set(ai.get("ai_pct_of_cycles", 0))

    for entry in data.get("jira_health", []):
        key = entry["jira_key"]
        if entry.get("cycle_failure_rate") is not None:
            JIRA_CI_FAILURE_RATE.labels(jira_key=key).set(entry["cycle_failure_rate"])
        if entry.get("retest_tax") is not None:
            JIRA_CI_RETEST_TAX.labels(jira_key=key).set(entry["retest_tax"])
        JIRA_CI_CYCLES.labels(jira_key=key).set(entry.get("total_cycles", 0))
        JIRA_CI_PR_COUNT.labels(jira_key=key).set(entry.get("pr_count", 0))

    for entry in data.get("jira_type_health", []):
        itype = entry["issue_type"]
        if entry.get("cycle_failure_rate") is not None:
            JIRA_TYPE_CI_FAILURE_RATE.labels(issue_type=itype).set(entry["cycle_failure_rate"])
        if entry.get("retest_tax") is not None:
            JIRA_TYPE_CI_RETEST_TAX.labels(issue_type=itype).set(entry["retest_tax"])

    for entry in data.get("jira_priority_health", []):
        prio = entry["priority"]
        if entry.get("cycle_failure_rate") is not None:
            JIRA_PRIORITY_CI_FAILURE_RATE.labels(priority=prio).set(entry["cycle_failure_rate"])
        if entry.get("retest_tax") is not None:
            JIRA_PRIORITY_CI_RETEST_TAX.labels(priority=prio).set(entry["retest_tax"])

    for entry in data.get("release_health", []):
        rel = entry["release"]
        if entry.get("cycle_failure_rate") is not None:
            REL_CI_FAILURE_RATE.labels(release=rel).set(entry["cycle_failure_rate"])
        if entry.get("retest_tax") is not None:
            REL_CI_RETEST_TAX.labels(release=rel).set(entry["retest_tax"])
        REL_CI_TOTAL_CYCLES.labels(release=rel).set(entry.get("total_cycles", 0))

    revert = data.get("revert_signals", {})
    REVERT_TOTAL_WITH_CI.set(revert.get("total_reverts_with_pr", 0))
    REVERT_CI_WARNED_PCT.set(revert.get("ci_warned_pct", 0))

    for entry in data.get("step_breakdown", []):
        comp = entry["component"]
        for s in entry.get("steps", []):
            COMP_STEP_FAILURES.labels(component=comp, step=s["step"]).set(s["failures"])

    for entry in data.get("cycle_duration_breakdown", []):
        comp = entry["component"]
        for cat in entry.get("breakdown", []):
            COMP_DURATION_BREAKDOWN.labels(
                component=comp, category=cat["category"],
            ).set(cat["avg_min"])

    for entry in data.get("infra_vs_code", []):
        comp = entry["component"]
        COMP_INFRA_FAILURE_PCT.labels(component=comp).set(entry.get("infra_pct", 0))
        COMP_CODE_FAILURE_PCT.labels(component=comp).set(entry.get("code_pct", 0))


_TABLE_DATA: dict[str, list[dict]] = {}


def _build_table_data(result: dict) -> None:
    """Pre-compute JSON table payloads from compute_all result."""
    data = result.get("git_ci_insights", {})

    step_breakdown_map: dict[str, str] = {}
    for sb in data.get("step_breakdown", []):
        steps = sb.get("steps", [])
        if steps:
            step_breakdown_map[sb["component"]] = steps[0]["step"]

    infra_map: dict[str, float] = {}
    for iv in data.get("infra_vs_code", []):
        infra_map[iv["component"]] = iv.get("infra_pct", 0)

    reason_map: dict[str, str] = {}
    for fr in data.get("failure_reasons", []):
        reasons = fr.get("top_reasons", [])
        if reasons:
            reason_map[fr["component"]] = reasons[0]["message"]

    rows = []
    for entry in data.get("component_health", []):
        comp = entry["component"]
        rows.append({
            "component": comp,
            "failure_rate": entry.get("cycle_failure_rate"),
            "retest_tax": entry.get("retest_tax"),
            "prs": entry.get("total_prs_with_ci", 0),
            "avg_duration_min": (entry.get("cycle_duration_minutes") or {}).get("mean"),
            "top_failing_step": step_breakdown_map.get(comp, ""),
            "infra_failure_pct": infra_map.get(comp, 0),
            "top_failure_reason": reason_map.get(comp, ""),
        })
    rows.sort(key=lambda r: r.get("failure_rate") or 0, reverse=True)
    _TABLE_DATA["component-health"] = rows

    rows = []
    for entry in data.get("_risk_summary", []):
        rows.append({
            "component": entry["component"],
            "critical_functions": entry.get("critical", 0),
            "high_risk_functions": entry.get("high", 0),
            "avg_risk_score": entry.get("avg_risk"),
        })
    rows.sort(key=lambda r: r.get("avg_risk_score") or 0, reverse=True)
    _TABLE_DATA["component-risk"] = rows

    jira_reason_map: dict[str, str] = {}
    for jr in data.get("jira_failure_reasons", []):
        reasons = jr.get("top_reasons", [])
        if reasons:
            jira_reason_map[jr["jira_key"]] = reasons[0]["message"]

    rows = []
    for entry in data.get("jira_health", []):
        jk = entry["jira_key"]
        rows.append({
            "jira_key": jk,
            "issue_type": entry.get("issue_type", ""),
            "priority": entry.get("priority", ""),
            "status": entry.get("status", ""),
            "summary": (entry.get("summary") or "")[:60],
            "failure_rate": entry.get("cycle_failure_rate"),
            "retest_tax": entry.get("retest_tax"),
            "ci_cycles": entry.get("total_cycles", 0),
            "prs": entry.get("pr_count", 0),
            "overridden_prs": entry.get("overridden_prs", 0),
            "top_failure_reason": jira_reason_map.get(jk, ""),
        })
    rows.sort(key=lambda r: r.get("failure_rate") or 0, reverse=True)
    _TABLE_DATA["jira-health"] = rows[:20]

    rows = []
    for entry in data.get("jira_type_health", []):
        rows.append({
            "issue_type": entry["issue_type"],
            "failure_rate": entry.get("cycle_failure_rate"),
            "retest_tax": entry.get("retest_tax"),
            "ci_cycles": entry.get("total_cycles", 0),
            "prs": entry.get("total_prs_with_ci", 0),
        })
    _TABLE_DATA["jira-type-health"] = rows

    rows = []
    for entry in data.get("jira_priority_health", []):
        rows.append({
            "priority": entry["priority"],
            "failure_rate": entry.get("cycle_failure_rate"),
            "retest_tax": entry.get("retest_tax"),
            "ci_cycles": entry.get("total_cycles", 0),
            "prs": entry.get("total_prs_with_ci", 0),
        })
    _TABLE_DATA["jira-priority-health"] = rows

    rows = []
    for entry in data.get("release_health", []):
        rows.append({
            "release": entry["release"],
            "failure_rate": entry.get("cycle_failure_rate"),
            "retest_tax": entry.get("retest_tax"),
            "ci_cycles": entry.get("total_cycles", 0),
        })
    _TABLE_DATA["release-health"] = rows

    ci = result.get("ci_efficiency", {})
    rows = []
    for w in ci.get("weekly_failures", []):
        total = w["total"]
        failures = w["failures"]
        rows.append({
            "week": w["week"],
            "total_cycles": total,
            "failures": failures,
            "passes": total - failures,
        })
    rows.sort(key=lambda r: r["week"])
    _TABLE_DATA["weekly-pass-fail"] = rows

    # Step failure breakdown per component
    rows = []
    for entry in data.get("step_breakdown", []):
        comp = entry["component"]
        for s in entry.get("steps", []):
            rows.append({
                "component": comp,
                "step": s["step"],
                "failures": s["failures"],
                "pct": s["pct"],
            })
    _TABLE_DATA["component-step-breakdown"] = rows

    # Cycle duration breakdown per component
    rows = []
    for entry in data.get("cycle_duration_breakdown", []):
        comp = entry["component"]
        for cat in entry.get("breakdown", []):
            rows.append({
                "component": comp,
                "category": cat["category"],
                "avg_min": cat["avg_min"],
                "pct": cat["pct"],
            })
    _TABLE_DATA["component-duration-breakdown"] = rows

    # Component failure reasons
    rows = []
    for entry in data.get("failure_reasons", []):
        comp = entry["component"]
        reasons = entry.get("top_reasons", [])
        top_reason = reasons[0]["message"] if reasons else ""
        rows.append({
            "component": comp,
            "top_reason": top_reason,
            "reason_count": reasons[0]["count"] if reasons else 0,
        })
    _TABLE_DATA["component-failure-reasons"] = rows

    # Jira failure reasons
    rows = []
    for entry in data.get("jira_failure_reasons", []):
        reasons = entry.get("top_reasons", [])
        top_reason = reasons[0]["message"] if reasons else ""
        rows.append({
            "jira_key": entry["jira_key"],
            "top_reason": top_reason,
            "reason_count": reasons[0]["count"] if reasons else 0,
        })
    _TABLE_DATA["jira-failure-reasons"] = rows

    # Weekly component failures — pivot to wide format for Grafana bar chart.
    # Input:  [{"week": "2026-03-09", "component": "cmd", "failures": 3}, ...]
    # Output: [{"week": "2026-03-09", "cmd": 3, "kserve": 5, ...}, ...]
    raw_weekly = data.get("weekly_component_failures", [])
    weekly_wide: dict[str, dict[str, int]] = {}
    for entry in raw_weekly:
        wk = entry["week"]
        if wk not in weekly_wide:
            weekly_wide[wk] = {"week": wk}
        weekly_wide[wk][entry["component"]] = entry["failures"]
    _TABLE_DATA["weekly-component-failures"] = sorted(
        weekly_wide.values(), key=lambda r: r["week"],
    )


def _build_stability_tables(store: Store) -> None:
    """Build test-stability, manifest-timeline, manifest-regressions, and
    test-regression-attribution table data from SQLite."""

    # --- test-stability ---
    try:
        test_rows = store.conn.execute("""
            SELECT tr.test_name,
                   COUNT(DISTINCT CASE WHEN tr.status = 'failed' THEN tr.build_id END) AS fail_builds,
                   COUNT(DISTINCT tr.build_id) AS total_builds,
                   MAX(CASE WHEN tr.status = 'failed' THEN cb.started_at END) AS last_failure
            FROM ci_test_results tr
            JOIN ci_builds cb ON cb.build_id = tr.build_id
            WHERE tr.is_leaf = 1
            GROUP BY tr.test_name
            HAVING fail_builds >= 2
        """).fetchall()
    except Exception:
        test_rows = []

    stability = []
    broken_count = 0
    flaky_count = 0
    for r in test_rows:
        total = r["total_builds"]
        fails = r["fail_builds"]
        if total == 0:
            continue
        rate = fails / total
        if rate > 0.8:
            cat = "broken"
            broken_count += 1
        elif rate >= 0.2:
            cat = "flaky"
            flaky_count += 1
        else:
            continue
        stability.append({
            "test_name": r["test_name"],
            "fail_builds": fails,
            "total_builds": total,
            "fail_rate": round(rate, 3),
            "category": cat,
            "last_failure": r["last_failure"] or "",
        })
    stability.sort(key=lambda x: x["fail_rate"], reverse=True)
    _TABLE_DATA["test-stability"] = stability

    for gauge in _STABILITY_GAUGES:
        gauge._metrics.clear()
    TEST_STABILITY_BROKEN.labels(period="all").set(broken_count)
    TEST_STABILITY_FLAKY.labels(period="all").set(flaky_count)

    # --- manifest-timeline ---
    # Exclude HEAD-baseline rows (pr_number IS NULL) — those are snapshots,
    # not PR-driven changes, and would clutter the table with empty PR columns.
    try:
        pin_rows = store.conn.execute("""
            SELECT component, pinned_sha, branch, pr_number, captured_at
            FROM component_manifest_pins
            WHERE pr_number IS NOT NULL
            ORDER BY captured_at DESC
            LIMIT 200
        """).fetchall()
    except Exception:
        pin_rows = []

    _TABLE_DATA["manifest-timeline"] = [
        {
            "component": r["component"],
            "pinned_sha": r["pinned_sha"][:12],
            "branch": r["branch"] or "",
            "pr_number": r["pr_number"],
            "captured_at": r["captured_at"],
        }
        for r in pin_rows
    ]

    # --- manifest-regressions ---
    try:
        manifest_prs = [
            dict(p) for p in store.conn.execute("""
                SELECT number, title, merged_at, merge_sha, changed_files, changed_components
                FROM merged_prs
                WHERE is_manifest_update = 1 AND merged_at IS NOT NULL
                ORDER BY merged_at DESC LIMIT 10
            """).fetchall()
        ]
        all_builds = [dict(b) for b in store.get_ci_builds()]
        all_steps = store.get_all_build_steps() if all_builds else []
        build_start_map = {
            b["build_id"]: b.get("started_at", "")
            for b in all_builds if b.get("started_at")
        }
        regressions = _detect_manifest_regressions(
            manifest_prs, all_builds, all_steps, build_start_map,
        )
    except Exception:
        log.debug("manifest-regressions table build failed", exc_info=True)
        regressions = []

    reg_rows = []
    for reg in regressions[:20]:
        mpr = reg.get("manifest_pr", {})
        reg_rows.append({
            "pr_number": mpr.get("number"),
            "merged_at": mpr.get("merged_at", ""),
            "title": (mpr.get("title") or "")[:80],
            "affected_step": reg["step"],
            "failure_rate_before": round(reg["before_rate"], 3),
            "failure_rate_after": round(reg["after_rate"], 3),
            "delta": round(reg["increase"], 3),
        })
    _TABLE_DATA["manifest-regressions"] = reg_rows
    MANIFEST_REGRESSIONS_TOTAL.labels(period="all").set(len(regressions))

    # --- test-regression-attribution ---
    # Include broken (>80%) and high-flaky (>50%) tests, sorted worst-first
    attr_candidates = [t for t in stability if t["fail_rate"] > 0.5]
    attr_candidates.sort(key=lambda t: t["fail_rate"], reverse=True)
    attr_names = [t["test_name"] for t in attr_candidates][:15]
    attributions = []
    if attr_names:
        try:
            all_test_results = [
                dict(t) for t in store.conn.execute(
                    "SELECT * FROM ci_test_results WHERE is_leaf = 1"
                ).fetchall()
            ]
            build_map = {b["build_id"]: b for b in all_builds}
            merged_prs = [dict(p) for p in store.get_merged_prs(base_branch="main")]
            for tname in attr_names:
                onset = _detect_regression_onset(
                    tname, all_test_results, build_map, merged_prs,
                )
                if onset and onset.get("causal_pr"):
                    attributions.append({
                        "test_name": tname,
                        "causal_pr": onset["causal_pr"],
                        "pr_title": (onset.get("pr_title") or "")[:80],
                        "onset_date": onset.get("onset_date", ""),
                        "confidence": onset.get("confidence", "low"),
                        "pattern": onset.get("pattern", ""),
                    })
        except Exception:
            log.debug("test-regression-attribution build failed", exc_info=True)

    _TABLE_DATA["test-regression-attribution"] = attributions


class _MetricsHandler(BaseHTTPRequestHandler):
    """Serves /metrics (Prometheus) and /api/tables/* (JSON)."""

    def do_GET(self):
        if self.path == "/metrics":
            output = generate_latest()
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPE_LATEST)
            self.end_headers()
            self.wfile.write(output)
        elif self.path.startswith("/api/tables/"):
            name = self.path.split("/api/tables/", 1)[1].rstrip("/")
            rows = _TABLE_DATA.get(name)
            if rows is None:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'{"error": "unknown table"}')
                return
            body = json.dumps(rows).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        pass


def start_server(cfg: dict, port: int = 9090, refresh_interval: int = 3600) -> None:
    """Start the combined Prometheus + JSON API server."""
    log.info("Starting exporter on :%d", port)
    server = HTTPServer(("", port), _MetricsHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    while True:
        try:
            store = Store(cfg["collection"]["cache_db"])
            lookback = cfg["collection"].get("lookback_days", 365)
            min_ver = cfg.get("per_release", {}).get("min_version", "3.0.0")
            result = compute_all(store, lookback_days=lookback, min_version=min_ver)
            _update_metrics(result)
            _build_table_data(result)
            _build_stability_tables(store)
            store.close()
            log.info("Metrics updated successfully")
        except Exception:
            log.exception("Failed to update metrics")
        time.sleep(refresh_interval)
