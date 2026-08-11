"""Structured JSON context export for AI agents.

Exports a machine-readable context bundle for a specific PR or the entire
codebase state, designed to be consumed by LLM-based agents for automated
test fix suggestions, PR risk scoring, and failure triage.
"""

from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from metrics import ci_efficiency
from reports.assertion_parser import parse_failure_message
from reports.failure_patterns import (
    _compute_flake_rate,
    _detect_manifest_regressions,
    _is_manifest_update_pr,
    _is_wrapper_message,
    _normalize_message,
    _test_name_to_file,
)
from reports.links import LinkBuilder, local_access_json
from store.db import Store

log = logging.getLogger(__name__)


def _parse_json_field(value: str | None) -> list:
    if not value:
        return []
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []


def export_pr_context(store: Store, pr_number: int,
                      links: LinkBuilder | None = None) -> dict:
    """Export full context for a single PR as a structured dict.

    Returns a JSON-serializable dict containing everything an AI agent
    needs to understand and potentially fix this PR's failures.
    When a LinkBuilder is provided, includes links to Prow logs, GitHub,
    and CI observability Grafana dashboards.
    """
    prs = store.get_merged_prs(base_branch="main")
    pr = next((p for p in prs if p["number"] == pr_number), None)
    if pr is None:
        return {"error": f"PR #{pr_number} not found", "pr_number": pr_number}

    builds = store.get_ci_builds(pr_number=pr_number)
    components = _parse_json_field(pr.get("changed_components"))
    jira_keys = _parse_json_field(pr.get("jira_keys"))
    changed_files = _parse_json_field(pr.get("changed_files"))

    cycles = ci_efficiency._derive_cycles(sorted(builds, key=lambda b: b["build_id"]))

    failed_build_ids = {b["build_id"] for b in builds if b["result"] == "failure"}
    all_steps = store.get_build_steps()
    pr_steps = [s for s in all_steps if s["build_id"] in failed_build_ids]
    all_msgs = store.get_build_failure_messages()
    pr_msgs = [m for m in all_msgs if m["build_id"] in failed_build_ids]

    step_failures = []
    for s in pr_steps:
        if s.get("level") == "Error":
            step_failures.append({
                "step": s["step_name"],
                "build_id": s["build_id"],
                "duration_seconds": s.get("duration_seconds"),
                "is_infrastructure": bool(s.get("is_infra")),
            })

    error_messages = []
    for m in pr_msgs:
        error_messages.append({
            "message": m["message"],
            "source": m.get("source"),
            "count": m.get("count", 1),
            "build_id": m["build_id"],
            "normalized": _normalize_message(m["message"]),
        })

    # Individual e2e test failures
    all_test_results = store.get_test_results(status="failed", leaf_only=True)
    pr_test_failures = [t for t in all_test_results if t["build_id"] in failed_build_ids]
    test_failure_entries = []
    for t in pr_test_failures:
        raw_msg = (t.get("failure_message") or "")[:2000] or None
        is_wrapper = _is_wrapper_message(raw_msg) if raw_msg else False
        entry: dict = {
            "test_name": t["test_name"],
            "test_file": _test_name_to_file(t["test_name"]),
            "suite": t.get("suite"),
            "test_variant": t.get("test_variant"),
            "build_id": t["build_id"],
            "duration_seconds": t.get("duration_seconds"),
            "failure_message": raw_msg,
            "is_wrapper_message": is_wrapper,
        }
        if raw_msg and not is_wrapper:
            parsed = parse_failure_message(raw_msg)
            entry["parsed_assertion"] = {
                "summary": parsed.summary,
                "timeout_seconds": parsed.timeout_seconds,
                "source_file": parsed.source_file,
                "source_line": parsed.source_line,
                "expected": parsed.expected,
                "root_cause": parsed.root_cause,
                "context": parsed.context,
            }
        test_failure_entries.append(entry)

    flake_info = _compute_flake_rate(builds) if builds else {}

    reverts = store.get_reverts()
    was_reverted = any(r.get("reverted_pr") == pr_number for r in reverts)

    code_risks = store.get_code_risk_scores()
    pr_risks = []
    for r in code_risks:
        if r.get("component") in components:
            pr_risks.append({
                "file": r["file"],
                "function": r["function"],
                "component": r["component"],
                "complexity": r.get("complexity"),
                "churn_30d": r.get("churn_30d"),
                "risk_score": r.get("risk_score"),
                "risk_band": r.get("risk_band"),
            })
    pr_risks.sort(key=lambda x: x.get("risk_score") or 0, reverse=True)

    # Manifest regression detection
    all_prs_for_manifest = store.get_merged_prs(base_branch="main")
    manifest_prs = [p for p in all_prs_for_manifest if _is_manifest_update_pr(p)]
    all_builds_for_manifest = store.get_ci_builds()
    all_steps_for_manifest = store.get_all_build_steps()
    build_start_map = {b["build_id"]: b.get("started_at") or "" for b in all_builds_for_manifest}
    manifest_regressions = _detect_manifest_regressions(
        manifest_prs, all_builds_for_manifest, all_steps_for_manifest, build_start_map,
    )
    pr_failing_steps = {s["step"] for s in step_failures}
    matched_regressions = [
        r for r in manifest_regressions
        if not r["is_infra"] and r["step"] in pr_failing_steps
    ]

    pr_links: dict = {}
    if links:
        pr_links["github"] = links.github_pr(pr_number)
        ci_obs = links.ci_obs_pr_overview(pr_number)
        if ci_obs:
            pr_links["ci_obs_overview"] = ci_obs

    build_entries = []
    for b in builds:
        entry: dict = {
            "build_id": b["build_id"],
            "job_name": b["job_name"],
            "result": b["result"],
            "duration_seconds": b.get("duration_seconds"),
            "started_at": b.get("started_at"),
        }
        if links:
            entry["links"] = {
                "prow": links.prow_build(pr_number, b["job_name"], b["build_id"]),
                "gcs_artifacts": links.gcs_artifacts(pr_number, b["job_name"], b["build_id"]),
                "gcs_build_log": links.gcs_build_log(pr_number, b["job_name"], b["build_id"]),
            }
            logs_url = links.ci_obs_logs(b["build_id"])
            if logs_url:
                entry["links"]["ci_obs_logs"] = logs_url
            tests_url = links.ci_obs_tests(b["build_id"])
            if tests_url:
                entry["links"]["ci_obs_tests"] = tests_url
            inv_url = links.ci_obs_investigation(b["build_id"])
            if inv_url:
                entry["links"]["ci_obs_investigation"] = inv_url
        build_entries.append(entry)

    build_job_map = {b["build_id"]: b["job_name"] for b in builds}
    for sf in step_failures:
        if links:
            job = build_job_map.get(sf["build_id"], "")
            sf["links"] = {
                "prow": links.prow_build(pr_number, job, sf["build_id"]),
                "gcs_artifacts": links.gcs_artifacts(pr_number, job, sf["build_id"]),
                "gcs_build_log": links.gcs_build_log(pr_number, job, sf["build_id"]),
            }
            logs_url = links.ci_obs_logs(sf["build_id"])
            if logs_url:
                sf["links"]["ci_obs_logs"] = logs_url

    jira_issues_enriched = []
    if jira_keys:
        jira_issue_map = store.get_jira_issue_map()
        for key in jira_keys:
            issue = jira_issue_map.get(key)
            if issue:
                jira_issues_enriched.append({
                    "key": key,
                    "summary": issue.get("summary"),
                    "type": issue.get("issue_type"),
                    "priority": issue.get("priority"),
                    "status": issue.get("status"),
                    "assignee": issue.get("assignee"),
                })
            else:
                jira_issues_enriched.append({"key": key})

    return {
        "schema_version": "1.2",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "pr": {
            "number": pr["number"],
            "title": pr.get("title"),
            "author": pr.get("author"),
            "merged_at": pr.get("merged_at"),
            "created_at": pr.get("created_at"),
            "additions": pr.get("additions", 0),
            "deletions": pr.get("deletions", 0),
            "is_ai_assisted": bool(pr.get("is_ai_assisted")),
            "components": components,
            "jira_keys": jira_keys,
            "jira_issues": jira_issues_enriched,
            "changed_files": changed_files,
            "was_reverted": was_reverted,
            "links": pr_links,
        },
        "ci": {
            "total_builds": len(builds),
            "total_cycles": len(cycles),
            "cycle_results": [c["result"] for c in cycles],
            "first_pass_result": cycles[0]["result"] if cycles else None,
            "total_ci_minutes": sum(c["duration_seconds"] for c in cycles) / 60 if cycles else 0,
            "flake_assessment": {
                "is_flaky": flake_info.get("flaky_prs", 0) > 0,
                "flake_rate": flake_info.get("flake_rate"),
            },
            "builds": build_entries,
        },
        "failures": {
            "step_failures": step_failures,
            "test_failures": test_failure_entries[:50],
            "error_messages": error_messages[:20],
            "infrastructure_failure_count": sum(1 for s in step_failures if s["is_infrastructure"]),
            "code_failure_count": sum(1 for s in step_failures if not s["is_infrastructure"]),
            "distinct_failing_tests": len({t["test_name"] for t in test_failure_entries}),
        },
        "manifest_regressions": [
            {
                "step": r["step"],
                "before_rate": round(r["before_rate"], 3),
                "after_rate": round(r["after_rate"], 3),
                "increase": round(r["increase"], 3),
                "manifest_pr": r["manifest_pr"]["number"],
                "manifest_pr_title": (r["manifest_pr"].get("title") or "")[:80],
                "manifest_merged_at": r["manifest_pr"].get("merged_at"),
            }
            for r in matched_regressions
        ],
        "code_risk": {
            "high_risk_functions": pr_risks[:10],
            "component_risk_summary": _component_risk_for(components, code_risks),
        },
        "agent_hints": _generate_agent_hints(
            step_failures, error_messages, flake_info, components, links, pr_number,
            manifest_regressions=matched_regressions,
            build_job_map=build_job_map,
        ),
        "local_access": local_access_json(links) if links else {},
    }


def _component_risk_for(components: list[str], all_risks: list[dict]) -> list[dict]:
    result = []
    for comp in components:
        comp_risks = [r for r in all_risks if r.get("component") == comp]
        if not comp_risks:
            continue
        result.append({
            "component": comp,
            "total_functions": len(comp_risks),
            "critical_count": sum(1 for r in comp_risks if r.get("risk_band") == "Critical"),
            "high_count": sum(1 for r in comp_risks if r.get("risk_band") == "High"),
            "avg_risk": round(
                sum(r.get("risk_score", 0) for r in comp_risks) / len(comp_risks), 2
            ),
        })
    return result


def _generate_agent_hints(
    step_failures: list[dict],
    error_messages: list[dict],
    flake_info: dict,
    components: list[str],
    links: LinkBuilder | None = None,
    pr_number: int | None = None,
    manifest_regressions: list[dict] | None = None,
    build_job_map: dict[str, str] | None = None,
) -> dict:
    """Generate structured hints for an AI agent to act on."""
    hints: dict = {
        "suggested_action": "investigate",
        "priority_areas": [],
        "skip_reasons": [],
        "investigation_links": [],
    }

    if manifest_regressions:
        reg_steps = [r["step"] for r in manifest_regressions]
        mpr_nums = sorted({r["manifest_pr"]["number"] for r in manifest_regressions})
        hints["suggested_action"] = "investigate_manifest_regression"
        hints["priority_areas"].insert(0, {
            "type": "manifest_regression",
            "affected_steps": reg_steps,
            "causal_manifest_prs": mpr_nums,
            "suggestion": (
                f"Steps {', '.join(reg_steps)} started failing after manifest update "
                f"PR(s) #{', #'.join(str(n) for n in mpr_nums)} merged. This PR's code "
                "is likely NOT the cause. Compare old and new image SHAs in "
                "get_all_manifests.sh or build/operands-map.yaml, then check the "
                "upstream component's changelog for breaking changes."
            ),
        })

    infra_only = all(s["is_infrastructure"] for s in step_failures) if step_failures else False
    if infra_only and step_failures:
        hints["suggested_action"] = "retest"
        hints["skip_reasons"].append(
            "All failing steps are infrastructure (provisioning/scheduling). "
            "Retest is more appropriate than code changes."
        )
        return hints

    if flake_info.get("flake_rate") and flake_info["flake_rate"] > 0.7:
        hints["suggested_action"] = "retest_then_investigate"
        hints["skip_reasons"].append(
            f"High flake rate ({flake_info['flake_rate']*100:.0f}%). "
            "Try retesting first; if failure persists, investigate."
        )

    if step_failures:
        code_steps = [s for s in step_failures if not s["is_infrastructure"]]
        for step in code_steps:
            hint_entry: dict = {
                "type": "failing_step",
                "step": step["step"],
                "suggestion": f"Search for test assertions in step '{step['step']}' "
                              f"that match the error messages below.",
            }
            if links and step.get("build_id"):
                logs = links.ci_obs_logs(step["build_id"])
                if logs:
                    hint_entry["logs_url"] = logs
            hints["priority_areas"].append(hint_entry)

    if error_messages:
        top_msgs = sorted(error_messages, key=lambda m: m["count"], reverse=True)[:3]
        for m in top_msgs:
            hints["priority_areas"].append({
                "type": "error_message",
                "message": m["message"][:200],
                "normalized": m["normalized"],
                "count": m["count"],
                "suggestion": f"Search the codebase for this assertion or condition. "
                              f"Check recent changes to {', '.join(components) if components else 'relevant files'}.",
            })

    if not hints["priority_areas"]:
        hints["suggested_action"] = "review_logs"
        suggestion = "No specific failure signals found in structured data."
        if links and step_failures:
            first_bid = step_failures[0].get("build_id")
            if first_bid:
                logs_url = links.ci_obs_logs(first_bid)
                if logs_url:
                    suggestion += f" Check raw CI logs: {logs_url}"
        hints["priority_areas"].append({
            "type": "general",
            "suggestion": suggestion,
        })

    if links:
        failed_bids = {s["build_id"] for s in step_failures}
        bjm = build_job_map or {}
        for bid in list(failed_bids)[:3]:
            entry: dict = {"build_id": bid}
            logs = links.ci_obs_logs(bid)
            if logs:
                entry["ci_obs_logs"] = logs
            inv = links.ci_obs_investigation(bid)
            if inv:
                entry["ci_obs_investigation"] = inv
            tests = links.ci_obs_tests(bid)
            if tests:
                entry["ci_obs_tests"] = tests
            job = bjm.get(bid, "")
            if job and pr_number:
                entry["gcs_artifacts"] = links.gcs_artifacts(pr_number, job, bid)
                entry["gcs_build_log"] = links.gcs_build_log(pr_number, job, bid)
            hints["investigation_links"].append(entry)

    return hints


def export_codebase_health(store: Store, lookback_days: int = 30,
                           links: LinkBuilder | None = None) -> dict:
    """Export codebase-wide CI health as a structured dict.

    Useful for AI agents that need to prioritize which PRs or components
    to investigate across the whole project.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    cutoff_str = cutoff.strftime("%Y-%m-%d")

    all_prs = store.get_merged_prs(base_branch="main")
    period_prs = [p for p in all_prs if (p.get("merged_at") or "") >= cutoff_str]
    period_pr_nums = {p["number"] for p in period_prs}

    all_builds = store.get_ci_builds()
    period_builds = [b for b in all_builds if b["pr_number"] in period_pr_nums]

    all_steps = store.get_all_build_steps()
    all_msgs = store.get_all_build_failure_messages()
    failed_bids = {b["build_id"] for b in period_builds if b["result"] == "failure"}

    pr_components: dict[int, list[str]] = {}
    for p in period_prs:
        comps = _parse_json_field(p.get("changed_components"))
        if comps:
            pr_components[p["number"]] = comps

    comp_builds: dict[str, list[dict]] = defaultdict(list)
    for b in period_builds:
        for comp in pr_components.get(b["pr_number"], ["unknown"]):
            comp_builds[comp].append(b)

    component_health = []
    for comp, cbuilds in sorted(comp_builds.items()):
        summary = ci_efficiency.compute_summary(cbuilds)
        flake = _compute_flake_rate(cbuilds)

        comp_failed_bids = {b["build_id"] for b in cbuilds if b["result"] == "failure"}
        comp_msgs = [m for m in all_msgs if m["build_id"] in comp_failed_bids]
        top_errors = []
        if comp_msgs:
            msg_counter: Counter[str] = Counter()
            for m in comp_msgs:
                msg_counter[_normalize_message(m["message"])] += m.get("count", 1)
            top_errors = [{"pattern": p, "count": c} for p, c in msg_counter.most_common(3)]

        component_health.append({
            "component": comp,
            "prs": summary.get("total_prs_with_ci", 0),
            "cycles": summary.get("total_cycles", 0),
            "failure_rate": summary.get("cycle_failure_rate"),
            "retest_tax": summary.get("retest_tax"),
            "flake_rate": flake.get("flake_rate"),
            "top_errors": top_errors,
        })

    component_health.sort(key=lambda x: x.get("failure_rate") or 0, reverse=True)

    failing_prs = []
    for p in period_prs:
        pr_builds = [b for b in period_builds if b["pr_number"] == p["number"]]
        if not any(b["result"] == "failure" for b in pr_builds):
            continue
        cycles = ci_efficiency._derive_cycles(sorted(pr_builds, key=lambda b: b["build_id"]))
        if not cycles:
            continue
        entry: dict = {
            "number": p["number"],
            "title": p.get("title"),
            "author": p.get("author"),
            "components": _parse_json_field(p.get("changed_components")),
            "cycles": len(cycles),
            "failed_cycles": sum(1 for c in cycles if c["result"] == "failure"),
            "first_pass_ok": cycles[0]["result"] == "success",
        }
        if links:
            entry["links"] = {
                "github": links.github_pr(p["number"]),
            }
            ci_obs = links.ci_obs_pr_overview(p["number"])
            if ci_obs:
                entry["links"]["ci_obs_overview"] = ci_obs
        failing_prs.append(entry)

    failing_prs.sort(key=lambda x: x["failed_cycles"], reverse=True)

    overall = ci_efficiency.compute_summary(period_builds)

    return {
        "schema_version": "1.0",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "period": {
            "lookback_days": lookback_days,
            "since": cutoff_str,
            "total_prs": len(period_prs),
            "total_builds": len(period_builds),
        },
        "overall_health": {
            "failure_rate": overall.get("cycle_failure_rate"),
            "first_pass_success_rate": overall.get("first_pass_success_rate"),
            "retest_tax": overall.get("retest_tax"),
        },
        "component_health": component_health,
        "failing_prs": failing_prs[:20],
        "triage_order": [
            {
                "pr_number": p["number"],
                "reason": _triage_reason(p),
            }
            for p in failing_prs[:10]
        ],
    }


def _triage_reason(pr_info: dict) -> str:
    if not pr_info["first_pass_ok"] and pr_info["failed_cycles"] == pr_info["cycles"]:
        return "All cycles failed — likely a genuine regression"
    if pr_info["failed_cycles"] > 3:
        return f"Failed {pr_info['failed_cycles']}/{pr_info['cycles']} cycles — high failure count"
    if not pr_info["first_pass_ok"]:
        return "First pass failed — may need code fix or could be flaky"
    return "Intermittent failures — possible flakiness"
