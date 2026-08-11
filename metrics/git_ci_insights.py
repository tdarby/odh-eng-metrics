"""Engineering Intelligence metrics: join git data with CI telemetry.

Provides insights that pure CI observability cannot: which components,
features (Jira), and code patterns cause the most CI pain.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta

from metrics import ci_efficiency
from store.db import Store

log = logging.getLogger(__name__)


def _parse_json_field(value: str | None) -> list:
    if not value:
        return []
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []


def compute_component_ci_health(store: Store) -> list[dict]:
    """CI metrics grouped by the component(s) each PR touched.

    A PR touching multiple components contributes its CI builds
    to each component it touched.
    """
    prs = store.get_merged_prs(base_branch="main")
    builds = store.get_ci_builds()
    if not builds:
        return []

    pr_components: dict[int, list[str]] = {}
    for p in prs:
        comps = _parse_json_field(p.get("changed_components"))
        if comps:
            pr_components[p["number"]] = comps

    component_builds: dict[str, list[dict]] = defaultdict(list)
    for b in builds:
        for comp in pr_components.get(b["pr_number"], []):
            component_builds[comp].append(b)

    results = []
    for comp, cbuilds in sorted(component_builds.items()):
        summary = ci_efficiency.compute_summary(cbuilds)
        results.append({
            "component": comp,
            **summary,
        })

    results.sort(key=lambda x: x.get("cycle_failure_rate") or 0, reverse=True)
    return results


def compute_code_hotspot_correlation(store: Store) -> dict:
    """Cross-reference code risk scores with CI outcomes.

    Groups PRs by the highest risk band of functions they touched,
    then computes CI failure rate for each band.
    """
    prs = store.get_merged_prs(base_branch="main")
    builds = store.get_ci_builds()
    risk_scores = store.get_code_risk_scores()

    if not risk_scores or not builds:
        return {"available": False, "by_risk_band": []}

    file_risk: dict[str, str] = {}
    for rs in risk_scores:
        f = rs["file"]
        existing = file_risk.get(f)
        band = rs.get("risk_band", "Low")
        if existing is None or _band_rank(band) > _band_rank(existing):
            file_risk[f] = band

    BAND_ORDER = {"Critical": 3, "High": 2, "Medium": 1, "Low": 0}

    pr_risk_band: dict[int, str] = {}
    for p in prs:
        files = _parse_json_field(p.get("changed_files"))
        best_band = "Low"
        for f in files:
            band = file_risk.get(f, "Low")
            if BAND_ORDER.get(band, 0) > BAND_ORDER.get(best_band, 0):
                best_band = band
        if files:
            pr_risk_band[p["number"]] = best_band

    band_builds: dict[str, list[dict]] = defaultdict(list)
    for b in builds:
        band = pr_risk_band.get(b["pr_number"])
        if band:
            band_builds[band].append(b)

    by_band = []
    for band in ["Critical", "High", "Medium", "Low"]:
        bbuilds = band_builds.get(band, [])
        if not bbuilds:
            by_band.append({"risk_band": band, "pr_count": 0, "failure_rate": None})
            continue
        summary = ci_efficiency.compute_summary(bbuilds)
        by_band.append({
            "risk_band": band,
            "pr_count": summary.get("total_prs_with_ci", 0),
            "failure_rate": summary.get("cycle_failure_rate"),
            "retest_tax": summary.get("retest_tax"),
        })

    return {"available": True, "by_risk_band": by_band}


def _band_rank(band: str) -> int:
    return {"Critical": 3, "High": 2, "Medium": 1, "Low": 0}.get(band, 0)


def compute_component_resource_cost(store: Store) -> list[dict]:
    """Aggregate CI resource consumption (CPU/memory) by component."""
    prs = store.get_merged_prs(base_branch="main")
    builds = store.get_ci_builds()

    pr_components: dict[int, list[str]] = {}
    for p in prs:
        comps = _parse_json_field(p.get("changed_components"))
        if comps:
            pr_components[p["number"]] = comps

    comp_cpu: dict[str, list[float]] = defaultdict(list)
    comp_mem: dict[str, list[float]] = defaultdict(list)

    for b in builds:
        for comp in pr_components.get(b["pr_number"], []):
            cpu = b.get("peak_cpu_cores")
            mem = b.get("peak_memory_bytes")
            dur = b.get("duration_seconds") or 0
            if cpu and dur > 0:
                comp_cpu[comp].append(cpu * dur / 3600)
            if mem and dur > 0:
                comp_mem[comp].append(mem * dur / (1024**3 * 3600))

    results = []
    for comp in sorted(set(comp_cpu) | set(comp_mem)):
        results.append({
            "component": comp,
            "cpu_hours": round(sum(comp_cpu.get(comp, [])), 1),
            "memory_gb_hours": round(sum(comp_mem.get(comp, [])), 1),
            "build_count": len(comp_cpu.get(comp, comp_mem.get(comp, []))),
        })

    results.sort(key=lambda x: x["cpu_hours"], reverse=True)
    return results


def compute_ai_ci_summary(store: Store) -> dict:
    """CI metrics for AI-assisted PRs."""
    prs = store.get_merged_prs(base_branch="main")
    builds = store.get_ci_builds()

    if not builds:
        return {"available": False}

    ai_pr_nums = {p["number"] for p in prs if p.get("is_ai_assisted")}
    total_pr_nums = {p["number"] for p in prs}

    ai_builds = [b for b in builds if b["pr_number"] in ai_pr_nums]
    all_summary = ci_efficiency.compute_summary(builds)
    ai_summary = ci_efficiency.compute_summary(ai_builds)

    total_cycles = all_summary.get("total_cycles", 0)
    ai_cycles = ai_summary.get("total_cycles", 0)

    return {
        "available": True,
        "ai_pr_count": len(ai_pr_nums),
        "total_pr_count": len(total_pr_nums),
        "ai_pct_of_prs": round(len(ai_pr_nums) / len(total_pr_nums) * 100, 1) if total_pr_nums else 0,
        "ai_pct_of_cycles": round(ai_cycles / total_cycles * 100, 1) if total_cycles else 0,
        "first_pass_rate": ai_summary.get("first_pass_success_rate"),
        "retest_tax": ai_summary.get("retest_tax"),
        "failure_rate": ai_summary.get("cycle_failure_rate"),
    }


def _pr_had_clean_pass(pr_builds: list[dict]) -> bool:
    """Return True if a PR ever had a cycle where all jobs passed."""
    pr_builds_sorted = sorted(pr_builds, key=lambda x: x["build_id"])
    cycles = ci_efficiency._derive_cycles(pr_builds_sorted)
    return any(c["result"] == "success" for c in cycles)


def compute_jira_ci_health(store: Store) -> list[dict]:
    """CI metrics grouped by Jira ticket, enriched with issue metadata."""
    prs = store.get_merged_prs(base_branch="main")
    builds = store.get_ci_builds()

    if not builds:
        return []

    jira_issue_map = store.get_jira_issue_map()

    pr_jira: dict[int, list[str]] = {}
    for p in prs:
        keys = _parse_json_field(p.get("jira_keys"))
        if keys:
            pr_jira[p["number"]] = keys

    pr_builds_map: dict[int, list[dict]] = defaultdict(list)
    for b in builds:
        pr_builds_map[b["pr_number"]].append(b)

    jira_builds: dict[str, list[dict]] = defaultdict(list)
    jira_prs: dict[str, set[int]] = defaultdict(set)
    for b in builds:
        for key in pr_jira.get(b["pr_number"], []):
            jira_builds[key].append(b)
            jira_prs[key].add(b["pr_number"])

    results = []
    for jira_key, jbuilds in jira_builds.items():
        summary = ci_efficiency.compute_summary(jbuilds)
        override_count = sum(
            1 for pr_num in jira_prs[jira_key]
            if not _pr_had_clean_pass(pr_builds_map[pr_num])
        )
        entry = {
            "jira_key": jira_key,
            "pr_count": summary.get("total_prs_with_ci", 0),
            "overridden_prs": override_count,
            **summary,
        }
        issue = jira_issue_map.get(jira_key)
        if issue:
            entry["issue_type"] = issue.get("issue_type")
            entry["priority"] = issue.get("priority")
            entry["status"] = issue.get("status")
            entry["summary"] = issue.get("summary")
            entry["assignee"] = issue.get("assignee")
            entry["story_points"] = issue.get("story_points")
        results.append(entry)

    results.sort(key=lambda x: x.get("cycle_failure_rate") or 0, reverse=True)
    return results[:20]


def compute_jira_issue_type_ci_health(store: Store) -> list[dict]:
    """CI health aggregated by JIRA issue type (Bug, Story, Task, etc.)."""
    prs = store.get_merged_prs(base_branch="main")
    builds = store.get_ci_builds()
    jira_issue_map = store.get_jira_issue_map()

    if not builds or not jira_issue_map:
        return []

    pr_jira: dict[int, list[str]] = {}
    for p in prs:
        keys = _parse_json_field(p.get("jira_keys"))
        if keys:
            pr_jira[p["number"]] = keys

    type_builds: dict[str, list[dict]] = defaultdict(list)
    for b in builds:
        for key in pr_jira.get(b["pr_number"], []):
            issue = jira_issue_map.get(key)
            if issue:
                itype = issue.get("issue_type") or "Unknown"
                type_builds[itype].append(b)

    results = []
    for itype, tbuilds in sorted(type_builds.items()):
        summary = ci_efficiency.compute_summary(tbuilds)
        results.append({"issue_type": itype, **summary})

    results.sort(key=lambda x: x.get("cycle_failure_rate") or 0, reverse=True)
    return results


def compute_jira_priority_ci_health(store: Store) -> list[dict]:
    """CI health aggregated by JIRA priority (Blocker, Critical, Major, etc.)."""
    prs = store.get_merged_prs(base_branch="main")
    builds = store.get_ci_builds()
    jira_issue_map = store.get_jira_issue_map()

    if not builds or not jira_issue_map:
        return []

    pr_jira: dict[int, list[str]] = {}
    for p in prs:
        keys = _parse_json_field(p.get("jira_keys"))
        if keys:
            pr_jira[p["number"]] = keys

    priority_builds: dict[str, list[dict]] = defaultdict(list)
    for b in builds:
        for key in pr_jira.get(b["pr_number"], []):
            issue = jira_issue_map.get(key)
            if issue:
                prio = issue.get("priority") or "Unknown"
                priority_builds[prio].append(b)

    results = []
    for prio, pbuilds in sorted(priority_builds.items()):
        summary = ci_efficiency.compute_summary(pbuilds)
        results.append({"priority": prio, **summary})

    results.sort(key=lambda x: x.get("cycle_failure_rate") or 0, reverse=True)
    return results


def compute_release_ci_health(store: Store) -> list[dict]:
    """Aggregate CI health for all PRs that landed in each release."""
    releases = store.get_releases()
    builds = store.get_ci_builds()

    if not builds or not releases:
        return []

    pr_builds: dict[int, list[dict]] = defaultdict(list)
    for b in builds:
        pr_builds[b["pr_number"]].append(b)

    results = []
    for rel in releases:
        tag = rel["tag"]
        # branch_arrivals stores tags with "tag:" prefix from branch_tracker
        arrivals = store.conn.execute(
            "SELECT DISTINCT pr_number FROM branch_arrivals WHERE branch = ? OR branch = ?",
            (tag, f"tag:{tag}"),
        ).fetchall()
        rel_pr_nums = {a["pr_number"] for a in arrivals}

        rel_builds = [b for pr in rel_pr_nums for b in pr_builds.get(pr, [])]
        if not rel_builds:
            continue

        summary = ci_efficiency.compute_summary(rel_builds)
        results.append({
            "release": tag,
            "published": rel["published"],
            **summary,
        })

    return results


def compute_revert_signals(store: Store) -> dict:
    """Check whether reverted PRs had CI failures that could have warned us."""
    reverts = store.get_reverts()
    builds = store.get_ci_builds()

    if not reverts:
        return {"total_reverts_with_pr": 0, "ci_warned_pct": 0, "details": []}

    pr_builds: dict[int, list[dict]] = defaultdict(list)
    for b in builds:
        pr_builds[b["pr_number"]].append(b)

    details = []
    ci_warned = 0
    for rev in reverts:
        rp = rev.get("reverted_pr")
        if not rp:
            continue
        pr_ci = pr_builds.get(rp, [])
        had_failures = any(b["result"] == "failure" for b in pr_ci)
        if had_failures:
            ci_warned += 1
        details.append({
            "revert_date": rev["date"],
            "reverted_pr": rp,
            "ci_had_failures": had_failures,
            "ci_build_count": len(pr_ci),
            "ci_failure_count": sum(1 for b in pr_ci if b["result"] == "failure"),
        })

    total = len(details)
    return {
        "total_reverts_with_pr": total,
        "ci_warned_pct": round(ci_warned / total * 100, 1) if total else 0,
        "details": details,
    }


def _build_pr_components(store: Store) -> dict[int, list[str]]:
    """Build a lookup from PR number to components touched."""
    prs = store.get_merged_prs(base_branch="main")
    result: dict[int, list[str]] = {}
    for p in prs:
        comps = _parse_json_field(p.get("changed_components"))
        if comps:
            result[p["number"]] = comps
    return result


def _build_build_to_pr(store: Store) -> dict[str, int]:
    """Build a lookup from build_id to PR number."""
    builds = store.get_ci_builds()
    return {b["build_id"]: b["pr_number"] for b in builds}


def compute_component_step_breakdown(store: Store) -> list[dict]:
    """Per-component breakdown of which steps fail.

    Joins ci_build_steps (level='Error') with merged_prs via build->PR->components.
    """
    pr_components = _build_pr_components(store)
    build_to_pr = _build_build_to_pr(store)
    steps = store.get_build_steps(level="Error")

    if not steps:
        return []

    comp_step_failures: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for s in steps:
        pr_num = build_to_pr.get(s["build_id"])
        if pr_num is None:
            continue
        for comp in pr_components.get(pr_num, []):
            comp_step_failures[comp][s["step_name"]] += 1

    results = []
    for comp, step_counts in sorted(comp_step_failures.items()):
        total = sum(step_counts.values())
        steps_list = [
            {"step": step, "failures": cnt, "pct": round(cnt / total * 100, 1)}
            for step, cnt in sorted(step_counts.items(), key=lambda x: x[1], reverse=True)
        ]
        results.append({
            "component": comp,
            "total_failures": total,
            "steps": steps_list[:10],
        })

    results.sort(key=lambda x: x["total_failures"], reverse=True)
    return results


def compute_cycle_duration_breakdown(store: Store) -> list[dict]:
    """Per-component breakdown of where CI time is spent.

    Categories: provisioning (infra steps), test_execution (non-infra),
    other (unaccounted time).
    """
    pr_components = _build_pr_components(store)
    build_to_pr = _build_build_to_pr(store)
    all_steps = store.get_all_build_steps()

    if not all_steps:
        return []

    build_steps: dict[str, list[dict]] = defaultdict(list)
    for s in all_steps:
        build_steps[s["build_id"]].append(s)

    comp_provisioning: dict[str, list[float]] = defaultdict(list)
    comp_test_exec: dict[str, list[float]] = defaultdict(list)
    comp_total: dict[str, list[float]] = defaultdict(list)

    for bid, steps_list in build_steps.items():
        pr_num = build_to_pr.get(bid)
        if pr_num is None:
            continue
        comps = pr_components.get(pr_num, [])
        if not comps:
            continue

        prov_sec = sum(s["duration_seconds"] or 0 for s in steps_list if s["is_infra"])
        test_sec = sum(s["duration_seconds"] or 0 for s in steps_list if not s["is_infra"])
        total_sec = prov_sec + test_sec

        for comp in comps:
            comp_provisioning[comp].append(prov_sec / 60)
            comp_test_exec[comp].append(test_sec / 60)
            comp_total[comp].append(total_sec / 60)

    results = []
    all_comps = set(comp_provisioning) | set(comp_test_exec)
    for comp in sorted(all_comps):
        prov_vals = comp_provisioning.get(comp, [])
        test_vals = comp_test_exec.get(comp, [])
        total_vals = comp_total.get(comp, [])
        if not total_vals:
            continue

        avg_total = sum(total_vals) / len(total_vals)
        avg_prov = sum(prov_vals) / len(prov_vals) if prov_vals else 0
        avg_test = sum(test_vals) / len(test_vals) if test_vals else 0

        breakdown = []
        if avg_total > 0:
            breakdown.append({
                "category": "test_execution",
                "avg_min": round(avg_test, 1),
                "pct": round(avg_test / avg_total * 100, 1) if avg_total else 0,
            })
            breakdown.append({
                "category": "provisioning",
                "avg_min": round(avg_prov, 1),
                "pct": round(avg_prov / avg_total * 100, 1) if avg_total else 0,
            })

        results.append({
            "component": comp,
            "avg_total_min": round(avg_total, 1),
            "build_count": len(total_vals),
            "breakdown": breakdown,
        })

    results.sort(key=lambda x: x["avg_total_min"], reverse=True)
    return results


def compute_infra_vs_code_failures(store: Store) -> list[dict]:
    """Per-component split of failures into infra vs code.

    A build failure is classified as 'infra' if any of its failing steps
    (level='Error') is an infrastructure step.
    """
    pr_components = _build_pr_components(store)
    build_to_pr = _build_build_to_pr(store)
    failed_steps = store.get_build_steps(level="Error")
    builds = store.get_ci_builds()

    if not failed_steps or not builds:
        return []

    failed_build_ids = {b["build_id"] for b in builds if b["result"] == "failure"}

    build_has_infra_failure: dict[str, bool] = {}
    for s in failed_steps:
        bid = s["build_id"]
        if bid not in failed_build_ids:
            continue
        if s["is_infra"]:
            build_has_infra_failure[bid] = True
        elif bid not in build_has_infra_failure:
            build_has_infra_failure[bid] = False

    comp_infra: dict[str, int] = defaultdict(int)
    comp_code: dict[str, int] = defaultdict(int)
    comp_total: dict[str, int] = defaultdict(int)

    for bid in failed_build_ids:
        pr_num = build_to_pr.get(bid)
        if pr_num is None:
            continue
        comps = pr_components.get(pr_num, [])
        is_infra = build_has_infra_failure.get(bid, False)
        for comp in comps:
            comp_total[comp] += 1
            if is_infra:
                comp_infra[comp] += 1
            else:
                comp_code[comp] += 1

    results = []
    for comp in sorted(comp_total):
        total = comp_total[comp]
        infra = comp_infra.get(comp, 0)
        code = comp_code.get(comp, 0)
        results.append({
            "component": comp,
            "total_failures": total,
            "infra_failures": infra,
            "infra_pct": round(infra / total * 100, 1) if total else 0,
            "code_failures": code,
            "code_pct": round(code / total * 100, 1) if total else 0,
        })

    results.sort(key=lambda x: x["total_failures"], reverse=True)
    return results


_GENERIC_FAILURE_RE = re.compile(
    r"^Some steps failed:|"
    r"^Failed to get pod .+ for lifecycle metrics$|"
    r"client rate limiter Wait returned an error",
)


def _is_generic_failure(msg: str) -> bool:
    return bool(_GENERIC_FAILURE_RE.search(msg))


def compute_component_failure_reasons(store: Store) -> list[dict]:
    """Top failure messages per component (top 3 each)."""
    pr_components = _build_pr_components(store)
    build_to_pr = _build_build_to_pr(store)
    all_msgs = store.get_all_build_failure_messages()

    if not all_msgs:
        return []

    comp_msg_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for m in all_msgs:
        if _is_generic_failure(m["message"]):
            continue
        pr_num = build_to_pr.get(m["build_id"])
        if pr_num is None:
            continue
        for comp in pr_components.get(pr_num, []):
            truncated = m["message"][:120]
            comp_msg_counts[comp][truncated] += m.get("count", 1)

    results = []
    for comp in sorted(comp_msg_counts):
        msg_counts = comp_msg_counts[comp]
        top = sorted(msg_counts.items(), key=lambda x: x[1], reverse=True)[:3]
        results.append({
            "component": comp,
            "top_reasons": [{"message": msg, "count": cnt} for msg, cnt in top],
        })

    results.sort(key=lambda x: sum(r["count"] for r in x["top_reasons"]), reverse=True)
    return results


def compute_jira_failure_reasons(store: Store) -> list[dict]:
    """Top failure messages per Jira ticket (top 3 each)."""
    prs = store.get_merged_prs(base_branch="main")
    build_to_pr = _build_build_to_pr(store)
    all_msgs = store.get_all_build_failure_messages()

    if not all_msgs:
        return []

    pr_jira: dict[int, list[str]] = {}
    for p in prs:
        keys = _parse_json_field(p.get("jira_keys"))
        if keys:
            pr_jira[p["number"]] = keys

    jira_msg_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for m in all_msgs:
        if _is_generic_failure(m["message"]):
            continue
        pr_num = build_to_pr.get(m["build_id"])
        if pr_num is None:
            continue
        for key in pr_jira.get(pr_num, []):
            truncated = m["message"][:120]
            jira_msg_counts[key][truncated] += m.get("count", 1)

    results = []
    for jira_key in sorted(jira_msg_counts):
        msg_counts = jira_msg_counts[jira_key]
        top = sorted(msg_counts.items(), key=lambda x: x[1], reverse=True)[:3]
        results.append({
            "jira_key": jira_key,
            "top_reasons": [{"message": msg, "count": cnt} for msg, cnt in top],
        })

    return results[:20]


def compute_weekly_component_failures(store: Store) -> list[dict]:
    """Weekly cycle failure counts grouped by component.

    Returns rows suitable for a stacked bar chart: one row per
    (week, component) pair.
    """
    pr_components = _build_pr_components(store)
    builds = store.get_ci_builds()

    if not builds:
        return []

    prs_data = store.get_merged_prs(base_branch="main")
    pr_merge_date = {p["number"]: p.get("merged_at") or "" for p in prs_data}

    pr_builds: dict[int, list[dict]] = defaultdict(list)
    for b in builds:
        pr_builds[b["pr_number"]].append(b)
    for pr_num in pr_builds:
        pr_builds[pr_num].sort(key=lambda x: x["build_id"])

    week_comp: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for pr_num, blist in pr_builds.items():
        comps = pr_components.get(pr_num, [])
        if not comps:
            continue
        cycles = ci_efficiency._derive_cycles(blist)
        fallback_date = pr_merge_date.get(pr_num, "")
        for c in cycles:
            if c["result"] != "failure":
                continue
            date_str = c.get("started_at") or fallback_date
            if not date_str:
                continue
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            monday = dt - timedelta(days=dt.weekday())
            week = monday.strftime("%Y-%m-%d")
            for comp in comps:
                week_comp[week][comp] += 1

    results = []
    for week in sorted(week_comp):
        for comp, count in sorted(week_comp[week].items()):
            results.append({"week": week, "component": comp, "failures": count})

    return results


def compute(store: Store) -> dict:
    """Compute all git-CI insight metrics."""
    log.info("Computing component CI health...")
    comp_health = compute_component_ci_health(store)

    log.info("Computing code hotspot correlation...")
    hotspots = compute_code_hotspot_correlation(store)

    log.info("Computing component resource cost...")
    resource_cost = compute_component_resource_cost(store)

    log.info("Computing AI CI summary...")
    ai_summary = compute_ai_ci_summary(store)

    log.info("Computing Jira CI health...")
    jira_health = compute_jira_ci_health(store)

    log.info("Computing Jira issue type CI health...")
    jira_type_health = compute_jira_issue_type_ci_health(store)

    log.info("Computing Jira priority CI health...")
    jira_priority_health = compute_jira_priority_ci_health(store)

    log.info("Computing release CI health...")
    release_health = compute_release_ci_health(store)

    log.info("Computing revert signals...")
    revert_signals = compute_revert_signals(store)

    log.info("Fetching component risk summary...")
    risk_summary = store.get_component_risk_summary()

    log.info("Computing component step breakdown...")
    step_breakdown = compute_component_step_breakdown(store)

    log.info("Computing cycle duration breakdown...")
    duration_breakdown = compute_cycle_duration_breakdown(store)

    log.info("Computing infra vs code failures...")
    infra_vs_code = compute_infra_vs_code_failures(store)

    log.info("Computing component failure reasons...")
    failure_reasons = compute_component_failure_reasons(store)

    log.info("Computing Jira failure reasons...")
    jira_failure_reasons = compute_jira_failure_reasons(store)

    log.info("Computing weekly component failures...")
    weekly_comp = compute_weekly_component_failures(store)

    return {
        "component_health": comp_health,
        "code_hotspots": hotspots,
        "component_resource_cost": resource_cost,
        "ai_summary": ai_summary,
        "jira_health": jira_health,
        "jira_type_health": jira_type_health,
        "jira_priority_health": jira_priority_health,
        "release_health": release_health,
        "revert_signals": revert_signals,
        "_risk_summary": risk_summary,
        "step_breakdown": step_breakdown,
        "cycle_duration_breakdown": duration_breakdown,
        "infra_vs_code": infra_vs_code,
        "failure_reasons": failure_reasons,
        "jira_failure_reasons": jira_failure_reasons,
        "weekly_component_failures": weekly_comp,
    }
