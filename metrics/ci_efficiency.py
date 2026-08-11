"""CI Efficiency metrics derived from CI Observability build data.

Computes first-pass success rate, retest tax, cycle failure rate,
CI duration per PR, monthly CI health trends, weekly failure trends,
and per-job failure breakdowns.

Key concept: a "test cycle" is one retest/push that triggers all CI jobs
for a PR in parallel.  Each cycle contains multiple job runs (e2e, unit,
lint, etc.).  Metrics here report at the cycle level, which is what
developers experience -- one cycle = one round of waiting for CI.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import datetime, timedelta

from store.db import Store


def _percentiles(values: list[float], pcts: tuple = (50, 90)) -> dict[str, float | None]:
    if not values:
        return {f"p{p}": None for p in pcts}
    sorted_vals = sorted(values)
    result = {}
    for p in pcts:
        idx = min(int(len(sorted_vals) * p / 100), len(sorted_vals) - 1)
        result[f"p{p}"] = round(sorted_vals[idx], 1)
    return result


def _monday_of_week(date_str: str) -> str:
    """Return 'YYYY-MM-DD' for the Monday of the week containing date_str."""
    dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    monday = dt - timedelta(days=dt.weekday())
    return monday.strftime("%Y-%m-%d")


def _short_job_name(job_name: str) -> str:
    """Extract short test type from a Prow job name.

    'pull-ci-opendatahub-io-opendatahub-operator-main-rhoai-e2e' -> 'rhoai-e2e'
    """
    for marker in ("-main-", "-master-"):
        idx = job_name.find(marker)
        if idx >= 0:
            return job_name[idx + len(marker):]
    parts = job_name.split("-")
    return "-".join(parts[-2:]) if len(parts) > 2 else job_name


def _derive_cycles(pr_builds: list[dict]) -> list[dict]:
    """Group a PR's job runs into test cycles.

    Prow triggers all required jobs for each push/retest.  The Nth run of
    each job_name belongs to the Nth cycle.  A cycle fails if ANY job in
    it failed; it succeeds only when ALL jobs passed.

    Duration is the max across parallel jobs (wall-clock wait time).
    started_at is the earliest build timestamp in the cycle.
    """
    by_job: dict[str, list[dict]] = defaultdict(list)
    for b in pr_builds:
        by_job[b["job_name"]].append(b)
    for job_builds in by_job.values():
        job_builds.sort(key=lambda x: x["build_id"])

    n_cycles = max(len(v) for v in by_job.values()) if by_job else 0

    cycles = []
    for i in range(n_cycles):
        results = []
        durations = []
        started_dates: list[str] = []
        for job_builds in by_job.values():
            if i < len(job_builds):
                results.append(job_builds[i]["result"])
                durations.append(job_builds[i]["duration_seconds"] or 0)
                sa = job_builds[i].get("started_at")
                if sa:
                    started_dates.append(sa)

        if "failure" in results:
            result = "failure"
        elif all(r == "success" for r in results):
            result = "success"
        else:
            result = "unknown"

        cycles.append({
            "result": result,
            "duration_seconds": max(durations) if durations else 0,
            "started_at": min(started_dates) if started_dates else None,
        })

    return cycles


def compute_summary(builds: list[dict]) -> dict:
    """Compute CI summary metrics from a pre-filtered list of builds.

    This is the stateless core used for period-scoped computation.
    It does not access the store or compute time-based trends.
    """
    if not builds:
        return {
            "available": False,
            "total_prs_with_ci": 0,
            "total_cycles": 0,
            "total_job_runs": 0,
            "first_pass_success_rate": None,
            "retest_tax": None,
            "cycle_failure_rate": None,
            "cycle_duration_minutes": {"count": 0, "mean": None, "p50": None, "p90": None},
            "ci_hours_per_pr": {"count": 0, "mean": None, "p50": None, "p90": None},
        }

    pr_builds: dict[int, list[dict]] = defaultdict(list)
    for b in builds:
        pr_builds[b["pr_number"]].append(b)
    for pr_num in pr_builds:
        pr_builds[pr_num].sort(key=lambda x: x["build_id"])

    total_prs = len(pr_builds)

    pr_cycles: dict[int, list[dict]] = {}
    for pr_num, blist in pr_builds.items():
        pr_cycles[pr_num] = _derive_cycles(blist)

    all_cycles = [c for cycles in pr_cycles.values() for c in cycles]
    total_cycles = len(all_cycles)

    first_pass_ok = sum(
        1 for cycles in pr_cycles.values()
        if cycles and cycles[0]["result"] == "success"
    )
    first_pass_rate = first_pass_ok / total_prs if total_prs else None
    retest_tax = total_cycles / total_prs if total_prs else None

    failed_cycles = sum(1 for c in all_cycles if c["result"] == "failure")
    cycle_failure_rate = failed_cycles / total_cycles if total_cycles else None

    cycle_durations_min = [
        c["duration_seconds"] / 60
        for c in all_cycles
        if c["duration_seconds"] > 0
    ]

    ci_hours_per_pr: list[float] = []
    for cycles in pr_cycles.values():
        total_sec = sum(c["duration_seconds"] for c in cycles)
        if total_sec > 0:
            ci_hours_per_pr.append(total_sec / 3600)

    return {
        "available": True,
        "total_prs_with_ci": total_prs,
        "total_cycles": total_cycles,
        "total_job_runs": len(builds),
        "first_pass_success_rate": round(first_pass_rate, 3) if first_pass_rate is not None else None,
        "retest_tax": round(retest_tax, 2) if retest_tax is not None else None,
        "cycle_failure_rate": round(cycle_failure_rate, 3) if cycle_failure_rate is not None else None,
        "cycle_duration_minutes": {
            "count": len(cycle_durations_min),
            "mean": round(statistics.mean(cycle_durations_min), 1) if cycle_durations_min else None,
            **_percentiles(cycle_durations_min),
        },
        "ci_hours_per_pr": {
            "count": len(ci_hours_per_pr),
            "mean": round(statistics.mean(ci_hours_per_pr), 1) if ci_hours_per_pr else None,
            **_percentiles(ci_hours_per_pr),
        },
    }


def compute(store: Store) -> dict:
    """Compute CI efficiency metrics from ci_builds data.

    Returns an empty result set when no CI data is available (stack not running).
    """
    builds = store.get_ci_builds()
    if not builds:
        return _empty_result()

    # Compute summary metrics (reused for period-scoped computation).
    summary = compute_summary(builds)

    # Unpack summary fields we need for trend computation below.
    pr_builds: dict[int, list[dict]] = defaultdict(list)
    for b in builds:
        pr_builds[b["pr_number"]].append(b)
    for pr_num in pr_builds:
        pr_builds[pr_num].sort(key=lambda x: x["build_id"])

    pr_cycles: dict[int, list[dict]] = {}
    for pr_num, blist in pr_builds.items():
        pr_cycles[pr_num] = _derive_cycles(blist)

    cycles_per_pr = [len(cycles) for cycles in pr_cycles.values()]
    cpp_distribution = _bucket_distribution(cycles_per_pr, [1, 2, 3, 4, 5, 10])

    # Build a lookup from PR number to merge date for time-based bucketing.
    prs_data = store.get_merged_prs(base_branch="main")
    pr_merge_date = {p["number"]: p.get("merged_at") or "" for p in prs_data}
    pr_merge_month = {n: d[:7] for n, d in pr_merge_date.items()}

    monthly: dict[str, dict] = defaultdict(lambda: {
        "cycles": 0, "failures": 0, "prs": set(),
    })
    for pr_num, cycles in pr_cycles.items():
        month = pr_merge_month.get(pr_num, "")
        if not month:
            continue
        monthly[month]["prs"].add(pr_num)
        for c in cycles:
            monthly[month]["cycles"] += 1
            if c["result"] == "failure":
                monthly[month]["failures"] += 1

    monthly_trends = []
    for month in sorted(monthly):
        m = monthly[month]
        n_cycles_m = m["cycles"]
        n_prs = len(m["prs"])
        monthly_trends.append({
            "month": month,
            "cycles": n_cycles_m,
            "prs": n_prs,
            "failures": m["failures"],
            "failure_pct": round(m["failures"] / n_cycles_m * 100, 1) if n_cycles_m else 0,
            "retest_tax": round(n_cycles_m / n_prs, 2) if n_prs else 0,
        })

    # --- Weekly cycle failure trend (panel 1) ---
    # Use build started_at when available, fall back to PR merge date.
    weekly_cycles_agg: dict[str, dict] = defaultdict(lambda: {"total": 0, "failures": 0})
    for pr_num, cycles in pr_cycles.items():
        fallback_date = pr_merge_date.get(pr_num, "")
        for c in cycles:
            date_str = c.get("started_at") or fallback_date
            if not date_str:
                continue
            week = _monday_of_week(date_str)
            weekly_cycles_agg[week]["total"] += 1
            if c["result"] == "failure":
                weekly_cycles_agg[week]["failures"] += 1

    weekly_failures = [
        {"week": w, "total": d["total"], "failures": d["failures"]}
        for w, d in sorted(weekly_cycles_agg.items())
    ]

    # --- Weekly failures by job / test type (panel 2) ---
    weekly_job_agg: dict[tuple[str, str], int] = defaultdict(int)
    for b in builds:
        if b["result"] != "failure":
            continue
        date_str = b.get("started_at") or pr_merge_date.get(b["pr_number"], "")
        if not date_str:
            continue
        week = _monday_of_week(date_str)
        short = _short_job_name(b["job_name"])
        weekly_job_agg[(week, short)] += 1

    weekly_job_failures: list[dict] = [
        {"week": k[0], "job": k[1], "failures": v}
        for k, v in sorted(weekly_job_agg.items())
    ]

    first_pass_rate = summary["first_pass_success_rate"]
    cycle_failure_rate = summary["cycle_failure_rate"]

    return {
        **summary,
        "first_pass_success_pct": f"{first_pass_rate * 100:.1f}%" if first_pass_rate is not None else "N/A",
        "cycle_failure_pct": f"{cycle_failure_rate * 100:.1f}%" if cycle_failure_rate is not None else "N/A",
        "cycles_per_pr_distribution": cpp_distribution,
        "monthly": monthly_trends,
        "weekly_failures": weekly_failures,
        "weekly_job_failures": weekly_job_failures,
        "builds": builds,
    }


def _empty_result() -> dict:
    return {
        "available": False,
        "total_prs_with_ci": 0,
        "total_cycles": 0,
        "total_job_runs": 0,
        "first_pass_success_rate": None,
        "first_pass_success_pct": "N/A",
        "retest_tax": None,
        "cycle_failure_rate": None,
        "cycle_failure_pct": "N/A",
        "ci_hours_per_pr": {"count": 0, "mean": None, "p50": None, "p90": None},
        "cycle_duration_minutes": {"count": 0, "mean": None, "p50": None, "p90": None},
        "cycles_per_pr_distribution": [],
        "monthly": [],
        "weekly_failures": [],
        "weekly_job_failures": [],
    }


def _bucket_distribution(values: list[int], thresholds: list[int]) -> list[dict]:
    """Bucket integer values into ranges like [1, 2, 3, 4, 5-9, 10+]."""
    buckets: list[dict] = []
    for t in thresholds[:-1]:
        buckets.append({"bucket": str(t), "count": 0})
    last_single = thresholds[-2] if len(thresholds) >= 2 else thresholds[0]
    last_upper = thresholds[-1]
    buckets.append({"bucket": f"{last_single + 1}-{last_upper - 1}", "count": 0})
    buckets.append({"bucket": f"{last_upper}+", "count": 0})

    for v in values:
        placed = False
        for i, t in enumerate(thresholds[:-1]):
            if v == t:
                buckets[i]["count"] += 1
                placed = True
                break
        if not placed:
            if v >= thresholds[-1]:
                buckets[-1]["count"] += 1
            elif v > thresholds[-2]:
                buckets[-2]["count"] += 1

    return [b for b in buckets if b["count"] > 0]
