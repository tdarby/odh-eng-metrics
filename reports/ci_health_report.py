"""Generate an HTML CI health report with embedded charts.

Produces a self-contained HTML file with matplotlib charts covering CI
efficiency, test health, infrastructure vs code failures, component health,
and weekly trends. Supports multiple time periods (week, month, 3 months)
shown side-by-side for comparison.
"""

from __future__ import annotations

import base64
import io
import json
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.ticker as mticker

import logging
logging.getLogger("matplotlib.category").setLevel(logging.ERROR)

from metrics.ci_efficiency import _derive_cycles, _short_job_name, compute_summary
from store.db import Store

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PERIOD_LABELS = {
    "week": "Last Working Week",
    "month": "Last Month",
    "3month": "Last 3 Months",
}


def _fig_to_base64(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode()


def _set_style(fig, ax):
    fig.set_facecolor("#1a1a2e")
    ax.set_facecolor("#16213e")
    ax.tick_params(colors="#e0e0e0")
    ax.xaxis.label.set_color("#e0e0e0")
    ax.yaxis.label.set_color("#e0e0e0")
    ax.title.set_color("#ffffff")
    for spine in ax.spines.values():
        spine.set_color("#333355")


def _pct(n, total) -> str:
    return f"{n / total * 100:.1f}%" if total else "N/A"


def _safe_pct(val) -> str:
    if val is None:
        return "N/A"
    return f"{val * 100:.1f}%"


def _monday(dt: datetime) -> str:
    return (dt - timedelta(days=dt.weekday())).strftime("%Y-%m-%d")


def _percentiles(values, pcts=(50, 90)):
    if not values:
        return {f"p{p}": None for p in pcts}
    s = sorted(values)
    return {f"p{p}": round(s[min(int(len(s) * p / 100), len(s) - 1)], 1) for p in pcts}


INFRA_STEP_PATTERNS = [
    "ipi-install", "ipi-deprovision", "baremetalds",
    "gather-", "lease-", "cluster-pool", "clusterclaim", "cucushift-pre",
    "hypershift-install", "openshift-cluster-bot-rbac",
    "projectdirectoryimagebuild", "inputimagetag",
    "importrelease", "bundlesource",
]


def _is_infra_step(name: str) -> bool:
    lower = name.lower()
    return any(p in lower for p in INFRA_STEP_PATTERNS)


# ---------------------------------------------------------------------------
# Data loading and period slicing
# ---------------------------------------------------------------------------

def _load_all_data(store: Store):
    """Load all raw data once; period slicing happens in _slice_period."""
    return {
        "all_builds": store.get_ci_builds(),
        "all_prs": store.get_merged_prs(base_branch="main"),
        "all_test_results": store.get_all_test_results(),
        "all_steps": store.get_all_build_steps(),
        "all_reverts": store.get_reverts(),
    }


def _slice_period(all_data: dict, days: int) -> dict:
    """Slice pre-loaded data for a time period ending now."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    builds = [b for b in all_data["all_builds"] if (b.get("started_at") or "") >= cutoff]
    prs = [p for p in all_data["all_prs"] if (p.get("merged_at") or "") >= cutoff]
    reverts = [r for r in all_data["all_reverts"] if (r.get("date") or "") >= cutoff]

    build_ids = {b["build_id"] for b in builds}
    test_results = [t for t in all_data["all_test_results"] if t["build_id"] in build_ids]
    steps = [s for s in all_data["all_steps"] if s["build_id"] in build_ids]

    return {
        "builds": builds,
        "prs": prs,
        "test_results": test_results,
        "steps": steps,
        "reverts": reverts,
        "cutoff": cutoff,
    }


def _slice_range(all_data: dict, start_iso: str, end_iso: str) -> dict:
    """Slice pre-loaded data for an explicit [start, end) time range."""
    builds = [b for b in all_data["all_builds"]
              if start_iso <= (b.get("started_at") or "") < end_iso]
    prs = [p for p in all_data["all_prs"]
           if start_iso <= (p.get("merged_at") or "") < end_iso]
    reverts = [r for r in all_data["all_reverts"]
               if start_iso <= (r.get("date") or "") < end_iso]

    build_ids = {b["build_id"] for b in builds}
    test_results = [t for t in all_data["all_test_results"] if t["build_id"] in build_ids]
    steps = [s for s in all_data["all_steps"] if s["build_id"] in build_ids]

    return {
        "builds": builds,
        "prs": prs,
        "test_results": test_results,
        "steps": steps,
        "reverts": reverts,
        "cutoff": start_iso,
    }


def _compute_period_metrics(data: dict) -> dict:
    """Compute all metrics for a single time period."""
    builds = data["builds"]
    prs = data["prs"]
    test_results = data["test_results"]
    steps = data["steps"]
    reverts = data["reverts"]

    summary = compute_summary(builds)

    # Test health: broken vs flaky vs passing
    build_ids = {b["build_id"] for b in builds}
    leaf_tests = [t for t in test_results if t.get("is_leaf", 1)]

    test_runs: dict[str, list[str]] = defaultdict(list)
    for t in leaf_tests:
        test_runs[t["test_name"]].append(t["status"])

    broken, flaky, healthy = [], [], []
    for name, statuses in test_runs.items():
        total = len(statuses)
        if total < 2:
            continue
        fail_rate = statuses.count("failed") / total
        if fail_rate > 0.8:
            broken.append({"name": name, "fail_rate": fail_rate, "runs": total,
                           "failures": statuses.count("failed")})
        elif fail_rate > 0.2:
            flaky.append({"name": name, "fail_rate": fail_rate, "runs": total,
                          "failures": statuses.count("failed")})
        else:
            healthy.append(name)

    broken.sort(key=lambda x: x["failures"], reverse=True)
    flaky.sort(key=lambda x: x["failures"], reverse=True)

    # Infra vs code failures
    failed_build_ids = {b["build_id"] for b in builds if b["result"] == "failure"}
    infra_failures = 0
    code_failures = 0
    for bid in failed_build_ids:
        bid_steps = [s for s in steps if s["build_id"] == bid and s.get("level") == "Error"]
        has_infra = any(s.get("is_infra") or _is_infra_step(s["step_name"]) for s in bid_steps)
        if has_infra:
            infra_failures += 1
        else:
            code_failures += 1

    # Wasted CI hours from failures
    wasted_hours = sum(
        (b.get("duration_seconds") or 0) / 3600
        for b in builds if b["result"] == "failure"
    )

    # Weekly trend within this period
    weekly: dict[str, dict] = defaultdict(lambda: {"total": 0, "failures": 0, "success": 0})
    pr_builds: dict[int, list[dict]] = defaultdict(list)
    for b in builds:
        pr_builds[b["pr_number"]].append(b)
    for pr_num in pr_builds:
        pr_builds[pr_num].sort(key=lambda x: x["build_id"])
    for pr_num, blist in pr_builds.items():
        for c in _derive_cycles(blist):
            date_str = c.get("started_at") or ""
            if not date_str:
                continue
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            week = _monday(dt)
            weekly[week]["total"] += 1
            if c["result"] == "failure":
                weekly[week]["failures"] += 1
            elif c["result"] == "success":
                weekly[week]["success"] += 1

    weekly_trend = [{"week": w, **d} for w, d in sorted(weekly.items())]

    # Weekly failures by job type
    weekly_jobs: dict[tuple[str, str], int] = defaultdict(int)
    for b in builds:
        if b["result"] != "failure":
            continue
        date_str = b.get("started_at") or ""
        if not date_str:
            continue
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        week = _monday(dt)
        short = _short_job_name(b["job_name"])
        weekly_jobs[(week, short)] += 1

    # Component health
    pr_components: dict[int, list[str]] = {}
    for p in prs:
        comps = json.loads(p.get("changed_components") or "[]")
        if comps:
            pr_components[p["number"]] = comps

    comp_builds: dict[str, list[dict]] = defaultdict(list)
    for b in builds:
        for comp in pr_components.get(b["pr_number"], ["unknown"]):
            comp_builds[comp].append(b)

    component_health = []
    for comp, cblds in sorted(comp_builds.items()):
        cs = compute_summary(cblds)
        if cs["total_cycles"] >= 3:
            component_health.append({"component": comp, **cs})
    component_health.sort(key=lambda x: x.get("cycle_failure_rate") or 0, reverse=True)

    # Top failing steps
    step_failures: Counter = Counter()
    for s in steps:
        if s.get("level") == "Error":
            step_failures[s["step_name"]] += 1

    return {
        "summary": summary,
        "broken_tests": broken[:15],
        "flaky_tests": flaky[:15],
        "healthy_test_count": len(healthy),
        "total_unique_tests": len(test_runs),
        "infra_failures": infra_failures,
        "code_failures": code_failures,
        "wasted_hours": round(wasted_hours, 1),
        "weekly_trend": weekly_trend,
        "weekly_jobs": dict(weekly_jobs),
        "component_health": component_health[:12],
        "top_failing_steps": step_failures.most_common(10),
        "reverts": len(reverts),
        "pr_count": len(prs),
    }


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------

COLORS = {
    "primary": "#4fc3f7",
    "success": "#66bb6a",
    "failure": "#ef5350",
    "warning": "#ffa726",
    "flaky": "#ffca28",
    "infra": "#ab47bc",
    "code": "#42a5f5",
    "muted": "#78909c",
    "bg": "#1a1a2e",
    "card": "#16213e",
    "text": "#e0e0e0",
    "accent1": "#26c6da",
    "accent2": "#7e57c2",
    "accent3": "#ec407a",
}


def chart_kpi_comparison(periods: dict[str, dict]) -> str:
    """Side-by-side KPI bars for each time period."""
    labels = list(periods.keys())
    kpis = ["first_pass_success_rate", "cycle_failure_rate", "retest_tax"]
    kpi_labels = ["First-Pass %", "Cycle Failure %", "Retest Tax"]
    kpi_colors = [COLORS["success"], COLORS["failure"], COLORS["warning"]]

    fig, axes = plt.subplots(1, 3, figsize=(14, 4))
    fig.set_facecolor(COLORS["bg"])

    for idx, (kpi, label, color) in enumerate(zip(kpis, kpi_labels, kpi_colors)):
        ax = axes[idx]
        ax.set_facecolor(COLORS["card"])
        vals = []
        for p in labels:
            v = periods[p]["summary"].get(kpi)
            if kpi != "retest_tax" and v is not None:
                v = v * 100
            vals.append(v or 0)

        period_names = [PERIOD_LABELS.get(p, p) for p in labels]
        bars = ax.barh(period_names, vals, color=color, height=0.5, alpha=0.85)
        ax.set_title(label, color="white", fontsize=11, fontweight="bold")
        for bar, v in zip(bars, vals):
            fmt = f"{v:.1f}%" if kpi != "retest_tax" else f"{v:.2f}x"
            ax.text(bar.get_width() + 0.5, bar.get_y() + bar.get_height() / 2,
                    fmt, va="center", color=COLORS["text"], fontsize=10)
        ax.tick_params(colors=COLORS["text"])
        for spine in ax.spines.values():
            spine.set_color("#333355")
        ax.invert_yaxis()

    fig.suptitle("CI Health KPIs by Period", color="white", fontsize=13, fontweight="bold", y=1.02)
    fig.tight_layout()
    return _fig_to_base64(fig)


def _parse_weeks(week_strs: list[str]) -> list[datetime]:
    return [datetime.strptime(w, "%Y-%m-%d") for w in week_strs]


def _format_week_axis(ax, n_weeks: int):
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    if n_weeks > 12:
        ax.xaxis.set_major_locator(mdates.WeekdayLocator(interval=2))
    ax.tick_params(axis="x", rotation=45)


def chart_weekly_failures(periods: dict[str, dict]) -> str:
    """Weekly cycle failure trend (stacked success/failure)."""
    longest = max(periods.values(), key=lambda p: len(p["weekly_trend"]))
    trend = longest["weekly_trend"]
    if not trend:
        return ""

    fig, ax = plt.subplots(figsize=(12, 4))
    _set_style(fig, ax)

    weeks = _parse_weeks([w["week"] for w in trend])
    successes = [w["success"] for w in trend]
    failures = [w["failures"] for w in trend]

    bar_width = 5
    ax.bar(weeks, successes, width=bar_width, color=COLORS["success"], alpha=0.8, label="Passed")
    ax.bar(weeks, failures, bottom=successes, width=bar_width, color=COLORS["failure"], alpha=0.8, label="Failed")

    ax.set_title("Weekly CI Cycles: Pass vs Fail", fontsize=12, fontweight="bold")
    ax.set_ylabel("Cycles")
    ax.legend(facecolor=COLORS["card"], edgecolor="#333355", labelcolor=COLORS["text"])
    _format_week_axis(ax, len(weeks))

    fig.tight_layout()
    return _fig_to_base64(fig)


def chart_weekly_failure_rate(periods: dict[str, dict]) -> str:
    """Weekly failure rate as a line chart."""
    longest = max(periods.values(), key=lambda p: len(p["weekly_trend"]))
    trend = longest["weekly_trend"]
    if not trend:
        return ""

    fig, ax = plt.subplots(figsize=(12, 4))
    _set_style(fig, ax)

    weeks = _parse_weeks([w["week"] for w in trend])
    rates = [w["failures"] / w["total"] * 100 if w["total"] else 0 for w in trend]

    ax.plot(weeks, rates, color=COLORS["failure"], linewidth=2, marker="o", markersize=4)
    ax.fill_between(weeks, rates, alpha=0.15, color=COLORS["failure"])

    avg_rate = statistics.mean(rates) if rates else 0
    ax.axhline(y=avg_rate, color=COLORS["warning"], linestyle="--", alpha=0.7, label=f"Avg: {avg_rate:.1f}%")

    ax.set_title("Weekly Cycle Failure Rate (%)", fontsize=12, fontweight="bold")
    ax.set_ylabel("Failure Rate %")
    ax.legend(facecolor=COLORS["card"], edgecolor="#333355", labelcolor=COLORS["text"])
    _format_week_axis(ax, len(weeks))

    fig.tight_layout()
    return _fig_to_base64(fig)


def chart_weekly_by_job(periods: dict[str, dict]) -> str:
    """Stacked bar chart of weekly failures by job type."""
    longest = max(periods.values(), key=lambda p: len(p["weekly_trend"]))
    weekly_jobs = longest["weekly_jobs"]
    if not weekly_jobs:
        return ""

    weeks_set: set[str] = set()
    jobs_set: set[str] = set()
    for (w, j), _ in weekly_jobs.items():
        weeks_set.add(w)
        jobs_set.add(j)

    week_strs = sorted(weeks_set)
    weeks = _parse_weeks(week_strs)
    jobs = sorted(jobs_set)
    job_colors = plt.cm.Set2.colors

    fig, ax = plt.subplots(figsize=(12, 5))
    _set_style(fig, ax)

    bar_width = 5
    bottoms = [0] * len(weeks)
    for i, job in enumerate(jobs):
        vals = [weekly_jobs.get((w, job), 0) for w in week_strs]
        color = job_colors[i % len(job_colors)]
        ax.bar(weeks, vals, bottom=bottoms, width=bar_width, label=job, color=color, alpha=0.85)
        bottoms = [b + v for b, v in zip(bottoms, vals)]

    ax.set_title("Weekly Failures by Job Type", fontsize=12, fontweight="bold")
    ax.set_ylabel("Failed Runs")
    ax.legend(facecolor=COLORS["card"], edgecolor="#333355", labelcolor=COLORS["text"],
              fontsize=8, loc="upper left")
    _format_week_axis(ax, len(weeks))

    fig.tight_layout()
    return _fig_to_base64(fig)


def chart_infra_vs_code(periods: dict[str, dict]) -> str:
    """Donut chart showing infrastructure vs code failures for each period."""
    n = len(periods)
    fig, axes = plt.subplots(1, n, figsize=(5 * n, 4))
    fig.set_facecolor(COLORS["bg"])
    if n == 1:
        axes = [axes]

    for ax, (period, metrics) in zip(axes, periods.items()):
        ax.set_facecolor(COLORS["bg"])
        infra = metrics["infra_failures"]
        code = metrics["code_failures"]
        total = infra + code

        if total == 0:
            ax.text(0.5, 0.5, "No failures", ha="center", va="center", color=COLORS["text"])
            ax.set_title(PERIOD_LABELS.get(period, period), color="white", fontsize=11)
            continue

        sizes = [infra, code]
        colors = [COLORS["infra"], COLORS["code"]]
        labels = [f"Infra ({infra})", f"Code/Test ({code})"]

        wedges, texts, autotexts = ax.pie(
            sizes, labels=labels, colors=colors, autopct="%1.0f%%",
            startangle=90, pctdistance=0.75,
            wedgeprops=dict(width=0.4, edgecolor=COLORS["bg"]),
            textprops=dict(color=COLORS["text"], fontsize=9),
        )
        for at in autotexts:
            at.set_color("white")
            at.set_fontsize(10)
        ax.set_title(PERIOD_LABELS.get(period, period), color="white", fontsize=11, fontweight="bold")

    fig.suptitle("Failure Root Cause: Infra vs Code", color="white", fontsize=13, fontweight="bold", y=1.02)
    fig.tight_layout()
    return _fig_to_base64(fig)


def chart_test_health(periods: dict[str, dict]) -> str:
    """Horizontal bar showing broken, flaky, healthy test counts per period."""
    n = len(periods)
    fig, axes = plt.subplots(1, n, figsize=(5 * n, 3.5))
    fig.set_facecolor(COLORS["bg"])
    if n == 1:
        axes = [axes]

    for ax, (period, metrics) in zip(axes, periods.items()):
        _set_style(fig, ax)
        categories = ["Broken (>80%)", "Flaky (20-80%)", "Healthy"]
        values = [len(metrics["broken_tests"]), len(metrics["flaky_tests"]),
                  metrics["healthy_test_count"]]
        colors = [COLORS["failure"], COLORS["flaky"], COLORS["success"]]

        bars = ax.barh(categories, values, color=colors, height=0.5, alpha=0.85)
        for bar, v in zip(bars, values):
            if v > 0:
                ax.text(bar.get_width() + 0.5, bar.get_y() + bar.get_height() / 2,
                        str(v), va="center", color=COLORS["text"], fontsize=10)
        ax.set_title(PERIOD_LABELS.get(period, period), color="white", fontsize=11, fontweight="bold")
        ax.invert_yaxis()

    fig.suptitle("Test Health Distribution", color="white", fontsize=13, fontweight="bold", y=1.02)
    fig.tight_layout()
    return _fig_to_base64(fig)


def chart_component_health(metrics: dict) -> str:
    """Horizontal bar chart of component cycle failure rates."""
    comps = metrics["component_health"]
    if not comps:
        return ""

    fig, ax = plt.subplots(figsize=(10, max(3, len(comps) * 0.5)))
    _set_style(fig, ax)

    names = [c["component"] for c in comps]
    rates = [(c.get("cycle_failure_rate") or 0) * 100 for c in comps]
    colors = [COLORS["failure"] if r > 50 else COLORS["warning"] if r > 30 else COLORS["success"]
              for r in rates]

    bars = ax.barh(names, rates, color=colors, height=0.6, alpha=0.85)
    for bar, r, c in zip(bars, rates, comps):
        cycles = c["total_cycles"]
        ax.text(bar.get_width() + 0.5, bar.get_y() + bar.get_height() / 2,
                f"{r:.0f}% ({cycles} cycles)", va="center", color=COLORS["text"], fontsize=9)
    ax.set_title("Component Cycle Failure Rate", fontsize=12, fontweight="bold")
    ax.set_xlabel("Failure Rate %")
    ax.invert_yaxis()
    fig.tight_layout()
    return _fig_to_base64(fig)


def chart_top_failing_steps(metrics: dict) -> str:
    """Horizontal bar chart of top failing CI steps."""
    steps = metrics["top_failing_steps"]
    if not steps:
        return ""

    fig, ax = plt.subplots(figsize=(10, max(3, len(steps) * 0.45)))
    _set_style(fig, ax)

    names = [s[0] for s in steps]
    counts = [s[1] for s in steps]
    colors = [COLORS["infra"] if _is_infra_step(n) else COLORS["code"] for n in names]

    bars = ax.barh(names, counts, color=colors, height=0.6, alpha=0.85)
    for bar, cnt in zip(bars, counts):
        ax.text(bar.get_width() + 0.3, bar.get_y() + bar.get_height() / 2,
                str(cnt), va="center", color=COLORS["text"], fontsize=9)
    ax.set_title("Top Failing CI Steps", fontsize=12, fontweight="bold")
    ax.set_xlabel("Error Count")
    ax.invert_yaxis()
    fig.tight_layout()
    return _fig_to_base64(fig)


def chart_cycle_duration(periods: dict[str, dict]) -> str:
    """Box-style chart showing cycle duration percentiles per period."""
    fig, ax = plt.subplots(figsize=(10, 4))
    _set_style(fig, ax)

    period_names = [PERIOD_LABELS.get(p, p) for p in periods]
    p50s = [periods[p]["summary"]["cycle_duration_minutes"].get("p50") or 0 for p in periods]
    p90s = [periods[p]["summary"]["cycle_duration_minutes"].get("p90") or 0 for p in periods]
    means = [periods[p]["summary"]["cycle_duration_minutes"].get("mean") or 0 for p in periods]

    x = range(len(period_names))
    width = 0.25

    ax.bar([i - width for i in x], means, width, label="Mean", color=COLORS["primary"], alpha=0.85)
    ax.bar(list(x), p50s, width, label="P50", color=COLORS["success"], alpha=0.85)
    ax.bar([i + width for i in x], p90s, width, label="P90", color=COLORS["warning"], alpha=0.85)

    ax.set_xticks(list(x))
    ax.set_xticklabels(period_names)
    ax.set_ylabel("Minutes")
    ax.set_title("Cycle Duration (Minutes)", fontsize=12, fontweight="bold")
    ax.legend(facecolor=COLORS["card"], edgecolor="#333355", labelcolor=COLORS["text"])

    for i, (m, p5, p9) in enumerate(zip(means, p50s, p90s)):
        for val, offset in [(m, -width), (p5, 0), (p9, width)]:
            if val > 0:
                ax.text(i + offset, val + 1, f"{val:.0f}", ha="center",
                        color=COLORS["text"], fontsize=9)

    fig.tight_layout()
    return _fig_to_base64(fig)


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------

def _stat_box(label: str, value: str, color: str = "#4fc3f7") -> str:
    return f"""<div class="stat-box">
        <div class="stat-value" style="color:{color}">{value}</div>
        <div class="stat-label">{label}</div>
    </div>"""


def _test_table(tests: list[dict], title: str) -> str:
    if not tests:
        return ""
    rows = "".join(
        f"<tr><td>{t['name']}</td><td>{t['failures']}/{t['runs']}</td>"
        f"<td>{t['fail_rate']:.0%}</td></tr>"
        for t in tests[:10]
    )
    return f"""<h4>{title}</h4>
    <table><thead><tr><th>Test Name</th><th>Fail/Total</th><th>Rate</th></tr></thead>
    <tbody>{rows}</tbody></table>"""


def generate(store: Store, output_path: str | Path = "data/ci-health-report.html") -> Path:
    """Generate the CI health HTML report."""
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    all_data = _load_all_data(store)

    period_days = {"week": 7, "month": 30, "3month": 90}
    period_metrics = {}

    for name, days in period_days.items():
        data = _slice_period(all_data, days)
        period_metrics[name] = _compute_period_metrics(data)

    # This week vs last week (ISO calendar weeks: Mon-Sun)
    now_utc = datetime.now(timezone.utc)
    this_monday = now_utc - timedelta(days=now_utc.weekday())
    this_monday = this_monday.replace(hour=0, minute=0, second=0, microsecond=0)
    last_monday = this_monday - timedelta(days=7)

    this_week_data = _slice_range(all_data, this_monday.isoformat(), now_utc.isoformat())
    last_week_data = _slice_range(all_data, last_monday.isoformat(), this_monday.isoformat())
    this_week_metrics = _compute_period_metrics(this_week_data)
    last_week_metrics = _compute_period_metrics(last_week_data)

    # Generate charts
    charts = {}
    charts["kpi_comparison"] = chart_kpi_comparison(period_metrics)
    charts["weekly_failures"] = chart_weekly_failures(period_metrics)
    charts["weekly_failure_rate"] = chart_weekly_failure_rate(period_metrics)
    charts["weekly_by_job"] = chart_weekly_by_job(period_metrics)
    charts["infra_vs_code"] = chart_infra_vs_code(period_metrics)
    charts["test_health"] = chart_test_health(period_metrics)
    charts["cycle_duration"] = chart_cycle_duration(period_metrics)
    charts["component_health"] = chart_component_health(period_metrics["month"])
    charts["top_failing_steps"] = chart_top_failing_steps(period_metrics["3month"])

    def img(key):
        b64 = charts.get(key, "")
        if not b64:
            return '<p class="no-data">No data available for this chart.</p>'
        return f'<img src="data:image/png;base64,{b64}" style="max-width:100%">'

    # Build executive summary stats
    week = period_metrics["week"]
    month = period_metrics["month"]
    three = period_metrics["3month"]

    now = datetime.now().strftime("%B %d, %Y")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>CI Health Report — {now}</title>
<style>
  body {{ font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
         background: #0f0f23; color: #e0e0e0; margin: 0; padding: 20px 40px;
         line-height: 1.6; }}
  h1 {{ color: #4fc3f7; border-bottom: 2px solid #333355; padding-bottom: 10px; }}
  h2 {{ color: #26c6da; margin-top: 40px; border-bottom: 1px solid #333355;
        padding-bottom: 6px; }}
  h3 {{ color: #7e57c2; }}
  h4 {{ color: #ec407a; margin-top: 20px; }}
  .subtitle {{ color: #78909c; font-size: 14px; margin-top: -10px; }}
  .stat-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
                gap: 12px; margin: 20px 0; }}
  .stat-box {{ background: #16213e; border: 1px solid #333355; border-radius: 8px;
               padding: 16px; text-align: center; }}
  .stat-value {{ font-size: 28px; font-weight: bold; }}
  .stat-label {{ font-size: 12px; color: #78909c; margin-top: 4px; }}
  table {{ border-collapse: collapse; width: 100%; margin: 12px 0; font-size: 13px; }}
  th, td {{ border: 1px solid #333355; padding: 8px 12px; text-align: left; }}
  th {{ background: #16213e; color: #4fc3f7; font-weight: 600; }}
  tr:nth-child(even) {{ background: rgba(22, 33, 62, 0.5); }}
  td {{ color: #e0e0e0; }}
  .toc {{ background: #16213e; border: 1px solid #333355; border-radius: 8px;
          padding: 16px 24px; margin: 20px 0; }}
  .toc ol {{ margin: 8px 0; padding-left: 20px; }}
  .toc a {{ color: #4fc3f7; text-decoration: none; }}
  .toc a:hover {{ text-decoration: underline; }}
  .insight {{ background: #1a2744; border-left: 4px solid #26c6da; padding: 12px 16px;
              margin: 12px 0; border-radius: 0 6px 6px 0; }}
  .warning {{ background: #2a1a00; border-left: 4px solid #ffa726; padding: 12px 16px;
              margin: 12px 0; border-radius: 0 6px 6px 0; }}
  .no-data {{ color: #78909c; font-style: italic; }}
  .wow-table td:last-child {{ font-weight: 600; font-size: 14px; }}
  .period-grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px;
                  margin: 20px 0; }}
  .period-card {{ background: #16213e; border: 1px solid #333355; border-radius: 8px;
                  padding: 16px; }}
  .period-card h3 {{ margin-top: 0; color: #4fc3f7; font-size: 14px; }}
  @media print {{ body {{ background: white; color: #333; }}
    .stat-box, .period-card, .toc {{ border-color: #ccc; background: #f9f9f9; }}
    .stat-value {{ color: #1565c0 !important; }} }}
</style>
</head>
<body>

<h1>CI Health Report</h1>
<p class="subtitle">Generated {now} &mdash; opendatahub-operator</p>

<div class="toc">
<strong>Contents</strong>
<ol>
  <li><a href="#exec">Executive Summary</a></li>
  <li><a href="#wow">This Week vs Last Week</a></li>
  <li><a href="#kpis">KPI Comparison</a></li>
  <li><a href="#weekly">Weekly Trends</a></li>
  <li><a href="#failures">Failure Analysis</a></li>
  <li><a href="#tests">Test Health</a></li>
  <li><a href="#components">Component Health</a></li>
  <li><a href="#duration">CI Duration</a></li>
  <li><a href="#steps">Failing Steps</a></li>
</ol>
</div>

<!-- ============================================================ -->
<h2 id="exec">1. Executive Summary</h2>

<div class="period-grid">
"""

    for period_key, label in PERIOD_LABELS.items():
        m = period_metrics[period_key]
        s = m["summary"]
        html += f"""<div class="period-card">
  <h3>{label}</h3>
  <div class="stat-grid">
    {_stat_box("PRs Merged", str(m["pr_count"]), COLORS["primary"])}
    {_stat_box("CI Cycles", str(s["total_cycles"]), COLORS["primary"])}
    {_stat_box("First-Pass %", _safe_pct(s["first_pass_success_rate"]), COLORS["success"])}
    {_stat_box("Failure Rate", _safe_pct(s["cycle_failure_rate"]), COLORS["failure"])}
    {_stat_box("Retest Tax", f'{s["retest_tax"]:.2f}x' if s["retest_tax"] else "N/A", COLORS["warning"])}
    {_stat_box("Wasted Hours", f'{m["wasted_hours"]:.0f}h', COLORS["failure"])}
    {_stat_box("Broken Tests", str(len(m["broken_tests"])), COLORS["failure"])}
    {_stat_box("Reverts", str(m["reverts"]), COLORS["accent3"])}
  </div>
</div>
"""

    html += "</div>"

    # Insights
    w_fpr = week["summary"].get("first_pass_success_rate")
    m_fpr = month["summary"].get("first_pass_success_rate")
    if w_fpr is not None and m_fpr is not None:
        delta = (w_fpr - m_fpr) * 100
        direction = "improved" if delta > 0 else "declined"
        color_class = "insight" if delta >= 0 else "warning"
        html += f"""<div class="{color_class}">
  <strong>Week vs Month:</strong> First-pass rate {direction} by {abs(delta):.1f} percentage points
  (week: {w_fpr*100:.1f}%, month avg: {m_fpr*100:.1f}%).
</div>"""

    if week["wasted_hours"] > 0:
        html += f"""<div class="warning">
  <strong>Wasted CI time this week:</strong> {week["wasted_hours"]:.0f} hours spent on failed cycles.
</div>"""

    # Week-over-week comparison section
    tw = this_week_metrics
    lw = last_week_metrics
    tw_s = tw["summary"]
    lw_s = lw["summary"]
    this_week_label = this_monday.strftime("%b %d")
    last_week_label = last_monday.strftime("%b %d")
    has_both_weeks = tw_s["total_cycles"] > 0 and lw_s["total_cycles"] > 0

    def _delta_indicator(current, previous, fmt="pct", higher_is_better=True):
        """Return an HTML span with arrow and color for a delta."""
        if current is None or previous is None:
            return '<span style="color:#78909c">—</span>'
        diff = current - previous
        if fmt == "pct":
            diff_display = f"{abs(diff * 100):.1f}pp"
        elif fmt == "float":
            diff_display = f"{abs(diff):.2f}"
        else:
            diff_display = f"{abs(diff):.0f}"
        if abs(diff) < 0.001:
            return '<span style="color:#78909c">&#x2194; no change</span>'
        is_improvement = (diff > 0) == higher_is_better
        arrow = "&#x25B2;" if diff > 0 else "&#x25BC;"
        color = "#66bb6a" if is_improvement else "#ef5350"
        return f'<span style="color:{color}">{arrow} {diff_display}</span>'

    html += f"""
<!-- ============================================================ -->
<h2 id="wow">This Week vs Last Week</h2>
<p class="subtitle">Calendar weeks: {last_week_label} &rarr; {this_week_label} (Mon&ndash;Sun)</p>
"""
    if not has_both_weeks:
        if tw_s["total_cycles"] == 0 and lw_s["total_cycles"] == 0:
            html += '<p class="no-data">No CI data for either week.</p>'
        elif lw_s["total_cycles"] == 0:
            html += '<p class="no-data">No CI data for last week — comparison not available.</p>'
        else:
            html += '<p class="no-data">No CI data for this week yet — comparison not available.</p>'
    else:
        html += f"""
<table>
<thead><tr>
    <th>Metric</th>
    <th>Last Week ({last_week_label})</th>
    <th>This Week ({this_week_label})</th>
    <th>Change</th>
</tr></thead>
<tbody>
<tr>
    <td>CI Cycles</td>
    <td>{lw_s["total_cycles"]}</td>
    <td>{tw_s["total_cycles"]}</td>
    <td>{_delta_indicator(tw_s["total_cycles"], lw_s["total_cycles"], fmt="int", higher_is_better=True)}</td>
</tr>
<tr>
    <td>First-Pass Rate</td>
    <td>{_safe_pct(lw_s.get("first_pass_success_rate"))}</td>
    <td>{_safe_pct(tw_s.get("first_pass_success_rate"))}</td>
    <td>{_delta_indicator(tw_s.get("first_pass_success_rate"), lw_s.get("first_pass_success_rate"), fmt="pct", higher_is_better=True)}</td>
</tr>
<tr>
    <td>Failure Rate</td>
    <td>{_safe_pct(lw_s.get("cycle_failure_rate"))}</td>
    <td>{_safe_pct(tw_s.get("cycle_failure_rate"))}</td>
    <td>{_delta_indicator(tw_s.get("cycle_failure_rate"), lw_s.get("cycle_failure_rate"), fmt="pct", higher_is_better=False)}</td>
</tr>
<tr>
    <td>Retest Tax</td>
    <td>{f'{lw_s["retest_tax"]:.2f}x' if lw_s.get("retest_tax") else 'N/A'}</td>
    <td>{f'{tw_s["retest_tax"]:.2f}x' if tw_s.get("retest_tax") else 'N/A'}</td>
    <td>{_delta_indicator(tw_s.get("retest_tax"), lw_s.get("retest_tax"), fmt="float", higher_is_better=False)}</td>
</tr>
<tr>
    <td>Wasted Hours</td>
    <td>{lw["wasted_hours"]:.0f}h</td>
    <td>{tw["wasted_hours"]:.0f}h</td>
    <td>{_delta_indicator(tw["wasted_hours"], lw["wasted_hours"], fmt="int", higher_is_better=False)}</td>
</tr>
<tr>
    <td>Broken Tests</td>
    <td>{len(lw["broken_tests"])}</td>
    <td>{len(tw["broken_tests"])}</td>
    <td>{_delta_indicator(len(tw["broken_tests"]), len(lw["broken_tests"]), fmt="int", higher_is_better=False)}</td>
</tr>
<tr>
    <td>Infra Failures</td>
    <td>{lw["infra_failures"]}</td>
    <td>{tw["infra_failures"]}</td>
    <td>{_delta_indicator(tw["infra_failures"], lw["infra_failures"], fmt="int", higher_is_better=False)}</td>
</tr>
<tr>
    <td>Code/Test Failures</td>
    <td>{lw["code_failures"]}</td>
    <td>{tw["code_failures"]}</td>
    <td>{_delta_indicator(tw["code_failures"], lw["code_failures"], fmt="int", higher_is_better=False)}</td>
</tr>
</tbody>
</table>
"""
        # Highlight the biggest improvement and biggest regression
        comparisons = [
            ("First-Pass Rate", tw_s.get("first_pass_success_rate"), lw_s.get("first_pass_success_rate"), True),
            ("Failure Rate", tw_s.get("cycle_failure_rate"), lw_s.get("cycle_failure_rate"), False),
            ("Wasted Hours", tw["wasted_hours"], lw["wasted_hours"], False),
            ("Broken Tests", len(tw["broken_tests"]), len(lw["broken_tests"]), False),
        ]
        improvements = []
        regressions = []
        for name, curr, prev, higher_better in comparisons:
            if curr is None or prev is None:
                continue
            diff = curr - prev
            is_improvement = (diff > 0) == higher_better
            if abs(diff) < 0.001:
                continue
            if is_improvement:
                improvements.append((name, curr, prev, diff))
            else:
                regressions.append((name, curr, prev, diff))

        if improvements:
            best = improvements[0]
            html += f"""<div class="insight">
  <strong>Improvement:</strong> {best[0]} got better this week.
</div>"""
        if regressions:
            worst = regressions[0]
            html += f"""<div class="warning">
  <strong>Regression:</strong> {worst[0]} got worse this week.
</div>"""

    # ----------------------------------------------------------------
    html += f"""
<!-- ============================================================ -->
<h2 id="kpis">3. KPI Comparison Across Periods</h2>
{img("kpi_comparison")}

<!-- ============================================================ -->
<h2 id="weekly">4. Weekly Trends</h2>

<h3>4.1 Cycle Pass/Fail Volume</h3>
{img("weekly_failures")}

<h3>4.2 Failure Rate Trend</h3>
{img("weekly_failure_rate")}

<h3>4.3 Failures by Job Type</h3>
{img("weekly_by_job")}

<!-- ============================================================ -->
<h2 id="failures">5. Failure Analysis</h2>

<h3>5.1 Infrastructure vs Code Failures</h3>
{img("infra_vs_code")}
"""

    # Infra/code insight
    for period_key, label in PERIOD_LABELS.items():
        m = period_metrics[period_key]
        total_f = m["infra_failures"] + m["code_failures"]
        if total_f > 0:
            infra_pct = m["infra_failures"] / total_f * 100
            html += f"""<div class="insight">
  <strong>{label}:</strong> {m["infra_failures"]} infra failures ({infra_pct:.0f}%) vs
  {m["code_failures"]} code/test failures ({100-infra_pct:.0f}%) out of {total_f} total.
</div>"""

    # ----------------------------------------------------------------
    html += f"""
<!-- ============================================================ -->
<h2 id="tests">6. Test Health</h2>
{img("test_health")}
"""

    for period_key, label in PERIOD_LABELS.items():
        m = period_metrics[period_key]
        html += f"<h3>{label}</h3>"
        html += _test_table(m["broken_tests"], "Broken Tests (>80% failure rate)")
        html += _test_table(m["flaky_tests"], "Flaky Tests (20-80% failure rate)")
        if not m["broken_tests"] and not m["flaky_tests"]:
            html += '<p class="no-data">No broken or flaky tests in this period.</p>'

    # ----------------------------------------------------------------
    html += f"""
<!-- ============================================================ -->
<h2 id="components">7. Component Health</h2>
<p>Component failure rates based on which components PRs touched (last month).</p>
{img("component_health")}
"""

    comp_health = period_metrics["month"]["component_health"]
    if comp_health:
        rows = "".join(
            f"<tr><td>{c['component']}</td>"
            f"<td>{c['total_prs_with_ci']}</td>"
            f"<td>{c['total_cycles']}</td>"
            f"<td>{_safe_pct(c.get('first_pass_success_rate'))}</td>"
            f"<td>{_safe_pct(c.get('cycle_failure_rate'))}</td>"
            f"<td>{c.get('retest_tax', 0):.2f}x</td></tr>"
            for c in comp_health
        )
        html += f"""<table>
<thead><tr><th>Component</th><th>PRs</th><th>Cycles</th><th>First-Pass %</th>
<th>Failure Rate</th><th>Retest Tax</th></tr></thead>
<tbody>{rows}</tbody></table>"""

    # ----------------------------------------------------------------
    html += f"""
<!-- ============================================================ -->
<h2 id="duration">8. CI Duration</h2>
{img("cycle_duration")}
"""

    for period_key, label in PERIOD_LABELS.items():
        m = period_metrics[period_key]
        dur = m["summary"]["cycle_duration_minutes"]
        ci_hr = m["summary"]["ci_hours_per_pr"]
        html += f"""<div class="insight">
  <strong>{label}:</strong>
  Cycle duration — mean: {dur.get('mean') or 'N/A'} min, p50: {dur.get('p50') or 'N/A'} min, p90: {dur.get('p90') or 'N/A'} min.
  CI hours/PR — mean: {ci_hr.get('mean') or 'N/A'}h, p50: {ci_hr.get('p50') or 'N/A'}h, p90: {ci_hr.get('p90') or 'N/A'}h.
</div>"""

    # ----------------------------------------------------------------
    html += f"""
<!-- ============================================================ -->
<h2 id="steps">9. Top Failing CI Steps (3 Months)</h2>
{img("top_failing_steps")}
"""

    steps = period_metrics["3month"]["top_failing_steps"]
    if steps:
        rows = "".join(
            f"<tr><td>{name}</td><td>{count}</td>"
            f"<td>{'Infra' if _is_infra_step(name) else 'Code/Test'}</td></tr>"
            for name, count in steps
        )
        html += f"""<table>
<thead><tr><th>Step</th><th>Error Count</th><th>Type</th></tr></thead>
<tbody>{rows}</tbody></table>"""

    html += """
</body>
</html>"""

    output.write_text(html)
    return output
