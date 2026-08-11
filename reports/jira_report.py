"""Per-collection JIRA analytics report.

Renders both structured JSON (for programmatic consumption) and
human-readable text output for terminal display.  When a Store is
provided and the collection uses the ``bug-bash`` analyzer, the
intelligence layer cross-references JIRA issues with PRs, CI, and
code risk data.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from metrics.jira_analytics import compute_bug_bash_intelligence, compute_collection_analytics

if TYPE_CHECKING:
    from store.db import Store


def generate(
    issues: list[dict],
    collection_name: str,
    collection_cfg: dict | None = None,
    store: Store | None = None,
) -> dict:
    """Compute analytics and return a structured result dict."""
    result = compute_collection_analytics(issues, collection_cfg)
    result["collection_name"] = collection_name
    result["description"] = (collection_cfg or {}).get("description", "")

    analyzer = (collection_cfg or {}).get("analyzer")
    if analyzer == "bug-bash" and store is not None:
        result["intelligence"] = compute_bug_bash_intelligence(
            issues, store, collection_cfg,
        )

    return result


def _fmt_hours(h: float | None) -> str:
    if h is None:
        return "N/A"
    if abs(h) < 48:
        return f"{h:.1f}h"
    return f"{h / 24:.1f}d"


def _wrap_text(text: str, width: int = 76, indent: str = "    ") -> str:
    """Wrap text to a given width, indenting continuation lines."""
    import textwrap
    lines = textwrap.wrap(text, width=width, subsequent_indent=indent)
    return "\n".join(lines)


def render_text(result: dict) -> str:
    """Render analytics result as human-readable text."""
    lines: list[str] = []

    name = result.get("collection_name", "unknown")
    desc = result.get("description", "")
    lines.append("")
    lines.append("=" * 60)
    lines.append(f"  JIRA Collection: {name}")
    if desc:
        lines.append(f"  {desc}")
    lines.append("=" * 60)

    if result.get("empty"):
        lines.append("  No issues in this collection.")
        return "\n".join(lines)

    # Project distribution (show early when multi-project)
    projects = result.get("project_distribution", [])
    if len(projects) > 1:
        lines.append("")
        lines.append("PROJECTS")
        lines.append("-" * 40)
        for entry in projects:
            lines.append(f"  {entry['name']:<20s} {entry['count']:>4d}  ({entry['pct']}%)")

    total = result["total"]
    res_rate = result["resolution_rate"]
    res_time = result.get("resolution_time_hours", {})

    lines.append("")
    lines.append("OVERVIEW")
    lines.append("-" * 40)
    lines.append(f"  Total issues:      {total}")
    lines.append(f"  Resolution rate:   {res_rate}%")
    if res_time.get("count", 0) > 0:
        lines.append(
            f"  Resolution time:   p50={_fmt_hours(res_time.get('p50'))}  "
            f"p90={_fmt_hours(res_time.get('p90'))}  mean={_fmt_hours(res_time.get('mean'))}"
        )

    aging = result.get("open_issue_aging_days", {})
    if aging.get("count", 0) > 0:
        lines.append(
            f"  Open issue aging:  p50={aging.get('p50', 'N/A')}d  "
            f"p90={aging.get('p90', 'N/A')}d  max={aging.get('max', 'N/A')}d  "
            f"({aging['count']} open)"
        )

    # Status category distribution
    status_cats = result.get("status_category_distribution", [])
    if status_cats:
        lines.append("")
        lines.append("STATUS")
        lines.append("-" * 40)
        for entry in status_cats:
            lines.append(f"  {entry['name']:<20s} {entry['count']:>4d}  ({entry['pct']}%)")

    # Type distribution
    types = result.get("type_distribution", [])
    if types:
        lines.append("")
        lines.append("ISSUE TYPES")
        lines.append("-" * 40)
        for entry in types:
            lines.append(f"  {entry['name']:<20s} {entry['count']:>4d}  ({entry['pct']}%)")

    # Priority distribution
    priorities = result.get("priority_distribution", [])
    if priorities:
        lines.append("")
        lines.append("PRIORITY")
        lines.append("-" * 40)
        for entry in priorities:
            lines.append(f"  {entry['name']:<20s} {entry['count']:>4d}  ({entry['pct']}%)")

    # Component distribution
    components = result.get("component_distribution", [])
    if components:
        lines.append("")
        lines.append("COMPONENTS")
        lines.append("-" * 40)
        for entry in components[:10]:
            lines.append(f"  {entry['name']:<25s} {entry['count']:>4d}  ({entry['pct']}%)")

    # Weekly throughput (recent)
    throughput = result.get("weekly_throughput", [])
    if throughput:
        recent = throughput[-8:]
        lines.append("")
        lines.append("WEEKLY RESOLUTION THROUGHPUT (recent)")
        lines.append("-" * 40)
        for w in recent:
            bar = "#" * w["resolved"]
            lines.append(f"  {w['week']}: {w['resolved']:>3d}  {bar}")

    # Specialized analyzer output
    specialized = result.get("specialized", {})
    analyzer = result.get("analyzer")
    if specialized.get("available") and analyzer == "bug-bash":
        summary = specialized.get("summary", {})

        lines.append("")
        lines.append("AI BUG BASH PIPELINE")
        lines.append("-" * 40)
        lines.append(f"  Triaged:              {summary.get('triaged', 0):>4d}")
        lines.append(f"    -> Fixable:          {summary.get('fixable', 0):>4d}")
        lines.append(f"    -> Not fixable:      {summary.get('nonfixable', 0):>4d}")
        lines.append(f"  Outcomes reached:     {summary.get('outcomes_reached', 0):>4d}  "
                     f"({summary.get('fixable_completion_pct', 0)}% of fixable)")
        lines.append(f"  Awaiting outcome:     {summary.get('awaiting_outcome', 0):>4d}")

        lines.append("")
        lines.append("AUTOMATION RATE (fully automated / fixable)")
        lines.append("-" * 40)
        lines.append(f"  Fully automated:      {summary.get('ai_automated_count', 0):>4d}")
        lines.append(f"  Fixable:              {summary.get('fixable', 0):>4d}")
        lines.append(f"  Automation rate:      {summary.get('automation_rate', 0):>5.1f}%")
        if summary.get("regressions", 0) > 0:
            lines.append(f"  Regressions found:    {summary['regressions']:>4d}")

        outcomes = specialized.get("outcomes", [])
        if outcomes:
            lines.append("")
            lines.append("OUTCOME BREAKDOWN")
            lines.append("-" * 40)
            for o in outcomes:
                if o["count"] == 0:
                    continue
                lines.append(
                    f"  {o['label']:<28s} {o['count']:>4d}  "
                    f"({o['pct_of_fixable']}% of fixable)"
                )

        triage_funnel = specialized.get("triage_funnel", [])
        if triage_funnel:
            lines.append("")
            lines.append("TRIAGE FUNNEL")
            lines.append("-" * 40)
            for stage in triage_funnel:
                lines.append(f"  {stage['stage']:<25s} {stage['count']:>4d}  ({stage['pct']}%)")

        by_project = specialized.get("by_project", [])
        if by_project and len(by_project) > 1:
            lines.append("")
            lines.append("BY PROJECT")
            lines.append("-" * 40)
            header = (
                f"  {'Project':<12s} {'Total':>5s} {'Triaged':>7s} "
                f"{'Fixable':>7s} {'Nonfixable':>10s} {'Auto':>5s} "
                f"{'Auto%':>6s} {'Accel':>5s} {'Accel%':>6s}"
            )
            lines.append(header)
            lines.append("  " + "-" * 68)
            for p in by_project:
                lines.append(
                    f"  {p['project']:<12s} {p['total']:>5d} {p['triaged']:>7d} "
                    f"{p['fixable']:>7d} {p['nonfixable']:>10d} {p['automated']:>5d} "
                    f"{p['automation_rate']:>5.1f}% {p['accelerated']:>5d} "
                    f"{p['accelerated_rate']:>5.1f}%"
                )

        severity = specialized.get("severity_profile", [])
        if severity:
            lines.append("")
            lines.append("SEVERITY PROFILE")
            lines.append("-" * 40)
            for s in severity:
                lines.append(f"  {s['name']:<20s} {s['count']:>4d}  ({s['pct']}%)")

    # --- Intelligence layer (cross-referenced analysis) ---
    intel = result.get("intelligence", {})
    if intel.get("available"):
        lines.append("")
        lines.append("")
        lines.append("=" * 60)
        lines.append("  BUG BASH INTELLIGENCE (cross-referenced)")
        lines.append("=" * 60)
        lines.append(f"  Linked PRs:  {intel.get('linked_prs', 0)}")

        _render_nonfixable(lines, intel.get("nonfixable_analysis", {}))
        _render_acceleration_gap(lines, intel.get("acceleration_gap", {}))
        _render_ci_impact(lines, intel.get("ci_impact", {}))
        _render_quality_signals(lines, intel.get("quality_signals", {}))
        _render_temporal(lines, intel.get("temporal", {}))
        _render_recommendations(lines, intel.get("recommendations", []))

    lines.append("")
    return "\n".join(lines)


def _render_nonfixable(lines: list[str], data: dict) -> None:
    if not data.get("available"):
        return
    lines.append("")
    lines.append("WHY TICKETS ARE NONFIXABLE")
    lines.append("=" * 40)
    lines.append(f"  {data['count']} issues marked ai-nonfixable")

    comps = data.get("by_component", [])
    if comps:
        lines.append("")
        lines.append("  BY COMPONENT")
        for c in comps[:8]:
            lines.append(f"    {c['name']:<25s} {c['count']:>3d}  ({c['pct']}%)")

    themes = data.get("themes", [])
    if themes:
        lines.append("")
        lines.append("  COMMON THEMES (from descriptions + comments)")
        for t in themes:
            lines.append(f'    "{t["theme"]}"  {t["count"]:>3d} issues  ({t["pct"]}%)')

    contrast = [c for c in data.get("component_contrast", []) if c.get("overrepresented")]
    if contrast:
        lines.append("")
        lines.append("  DISPROPORTIONATELY NONFIXABLE COMPONENTS")
        for c in contrast[:5]:
            lines.append(
                f"    {c['component']:<25s} nonfixable: {c['nonfixable_pct']}%  "
                f"vs fixable: {c['fixable_pct']}%"
            )

    risks = data.get("code_risk_overlap", [])
    if risks:
        lines.append("")
        lines.append("  CODE RISK OVERLAP")
        for r in risks[:5]:
            lines.append(
                f"    {r['component']:<25s} avg risk: {r['avg_risk_score']:.1f}  "
                f"high-risk fns: {r['high_risk_functions']}"
            )


def _render_acceleration_gap(lines: list[str], data: dict) -> None:
    if not data.get("available"):
        return
    lines.append("")
    lines.append("ACCELERATED-FIX -> FULLY-AUTOMATED GAP")
    lines.append("=" * 40)
    lines.append(f"  {data['count']} issues required multiple AI attempts")
    lines.append(f"  {data.get('total_prs', 0)} associated PRs")

    fpa = data.get("first_pass_analysis", {})
    if fpa.get("total_prs_with_builds"):
        lines.append("")
        lines.append("  FIRST-ATTEMPT CI RESULTS")
        lines.append(f"    PRs with builds:   {fpa['total_prs_with_builds']}")
        lines.append(f"    First-pass pass:   {fpa['first_pass_successes']}")
        lines.append(f"    First-pass fail:   {fpa['first_pass_failures']}")
        lines.append(f"    First-pass rate:   {fpa['first_pass_rate']}%")
        lines.append(f"    Code failures:     {fpa['code_failures']}")
        lines.append(f"    Infra failures:    {fpa['infra_failures']}")

    tests = data.get("top_failing_tests", [])
    if tests:
        lines.append("")
        lines.append("  TOP FAILING TESTS (blocking first-attempt success)")
        for t in tests[:5]:
            lines.append(f"    {t['test']:<50s} {t['count']:>2d} issues")

    hotspots = [h for h in data.get("component_hotspots", []) if h["multi_attempt"] > 0]
    if hotspots:
        lines.append("")
        lines.append("  COMPONENT HOTSPOTS (multi-attempt vs single-shot)")
        for h in hotspots[:8]:
            lines.append(
                f"    {h['component']:<25s} multi: {h['multi_attempt']}  "
                f"single: {h['single_shot']}  ({h['multi_attempt_pct']}% multi)"
            )

    size = data.get("pr_size_comparison", {})
    if size.get("accelerated_mean") is not None:
        lines.append("")
        lines.append("  PR SIZE (lines changed)")
        lines.append(f"    Accelerated-fix mean:  {size['accelerated_mean']:.0f}")
        if size.get("automated_mean") is not None:
            lines.append(f"    Fully-automated mean:  {size['automated_mean']:.0f}")

    multi = data.get("multi_pr_issues", [])
    if multi:
        lines.append("")
        lines.append(f"  ISSUES WITH MULTIPLE PRs ({len(multi)})")
        for m in multi[:5]:
            lines.append(f"    {m['key']}: {m['pr_count']} PRs — {m.get('summary', '')[:60]}")


def _render_ci_impact(lines: list[str], data: dict) -> None:
    bb = data.get("bug_bash", {})
    bl = data.get("baseline", {})
    if not bb.get("available"):
        return
    lines.append("")
    lines.append("CI IMPACT: BUG BASH vs BASELINE")
    lines.append("=" * 40)
    header = f"  {'Metric':<30s} {'Bug Bash':>10s} {'Baseline':>10s}"
    lines.append(header)
    lines.append("  " + "-" * 52)

    def _val(d: dict, key: str, fmt: str = "{}") -> str:
        v = d.get(key)
        if v is None:
            return "N/A"
        return fmt.format(v)

    lines.append(f"  {'PRs':<30s} {_val(bb, 'total_prs'):>10s} {_val(bl, 'total_prs'):>10s}")
    lines.append(f"  {'Total builds':<30s} {_val(bb, 'total_builds'):>10s} {_val(bl, 'total_builds'):>10s}")
    lines.append(f"  {'First-pass success %':<30s} {_val(bb, 'first_pass_success_pct', '{:.1f}%'):>10s} {_val(bl, 'first_pass_success_pct', '{:.1f}%'):>10s}")
    lines.append(f"  {'Retest tax (builds/PR)':<30s} {_val(bb, 'retest_tax', '{:.2f}'):>10s} {_val(bl, 'retest_tax', '{:.2f}'):>10s}")
    lines.append(f"  {'Build failure %':<30s} {_val(bb, 'failure_pct', '{:.1f}%'):>10s} {_val(bl, 'failure_pct', '{:.1f}%'):>10s}")
    lines.append(f"  {'Wasted CI hours':<30s} {_val(bb, 'wasted_ci_hours', '{:.1f}'):>10s} {_val(bl, 'wasted_ci_hours', '{:.1f}'):>10s}")
    lines.append(f"  {'Infra failures':<30s} {_val(bb, 'infra_failures'):>10s} {_val(bl, 'infra_failures'):>10s}")
    lines.append(f"  {'Code failures':<30s} {_val(bb, 'code_failures'):>10s} {_val(bl, 'code_failures'):>10s}")


def _render_quality_signals(lines: list[str], data: dict) -> None:
    if not data:
        return
    lines.append("")
    lines.append("CODE QUALITY SIGNALS")
    lines.append("=" * 40)
    lines.append(f"  Revert rate:        {data.get('revert_rate_pct', 0):.1f}%")
    if data.get("reverted_prs"):
        lines.append(f"  Reverted PRs:       {data['reverted_prs']}")
    lines.append(f"  Regressions found:  {data.get('regressions_found', 0)}")

    sizes = data.get("pr_size_comparison", {})
    ai_s = sizes.get("ai_success", {})
    ai_f = sizes.get("ai_failure", {})
    if ai_s.get("count") or ai_f.get("count"):
        lines.append("")
        lines.append("  PR SIZE: AI SUCCESS vs AI FAILURE")
        if ai_s.get("count"):
            lines.append(
                f"    AI success ({ai_s['count']} PRs):  mean={ai_s.get('mean', 'N/A')} LOC  "
                f"p50={ai_s.get('p50', 'N/A')}  p90={ai_s.get('p90', 'N/A')}"
            )
        if ai_f.get("count"):
            lines.append(
                f"    AI failure ({ai_f['count']} PRs):  mean={ai_f.get('mean', 'N/A')} LOC  "
                f"p50={ai_f.get('p50', 'N/A')}  p90={ai_f.get('p90', 'N/A')}"
            )

    risks = data.get("high_risk_touches", [])
    if risks:
        lines.append("")
        lines.append("  HIGH-RISK CODE TOUCHED BY BUG BASH FIXES")
        for r in risks[:5]:
            lines.append(
                f"    {r['component']:<25s} {r['pr_touches']} PRs  "
                f"avg risk: {r['avg_risk']:.1f}  high-risk fns: {r['high_risk_functions']}"
            )


def _render_temporal(lines: list[str], data: dict) -> None:
    if not data:
        return

    daily = data.get("daily_throughput", [])
    if daily:
        lines.append("")
        lines.append("DAILY THROUGHPUT")
        lines.append("=" * 40)
        for d in daily:
            bd = d.get("breakdown", {})
            detail = "  ".join(f"{k}:{v}" for k, v in sorted(bd.items()) if v > 0)
            lines.append(
                f"  {d['date']} ({d.get('day_name', '')[:3]}):  "
                f"{d['total_outcomes']:>3d} outcomes  {detail}"
            )

    fix_times = data.get("fix_time_by_outcome", {})
    if fix_times:
        lines.append("")
        lines.append("TIME-TO-FIX BY OUTCOME (hours)")
        lines.append("  " + "-" * 50)
        for outcome, stats in sorted(fix_times.items()):
            if stats.get("count", 0) > 0:
                lines.append(
                    f"  {outcome:<28s} n={stats['count']:>3d}  "
                    f"mean={_fmt_hours(stats.get('mean'))}  "
                    f"p50={_fmt_hours(stats.get('p50'))}  p90={_fmt_hours(stats.get('p90'))}"
                )

    dow = data.get("day_of_week_effectiveness", [])
    if dow and any(d["total_resolved"] > 0 for d in dow):
        lines.append("")
        lines.append("DAY-OF-WEEK EFFECTIVENESS")
        lines.append("  " + "-" * 50)
        for d in dow:
            if d["total_resolved"] > 0:
                bar = "#" * d["total_resolved"]
                lines.append(
                    f"  {d['day']:<12s} {d['total_resolved']:>3d} resolved  "
                    f"{d['ai_successes']:>3d} AI wins  "
                    f"({d['success_rate']}%)  {bar}"
                )


def _render_recommendations(lines: list[str], recs: list[dict]) -> None:
    if not recs:
        return
    lines.append("")
    lines.append("RECOMMENDATIONS")
    lines.append("=" * 60)
    for r in recs:
        sev = r["severity"].upper()
        prefix = {"ACTION": "[action]", "WARNING": "[warn]  ", "INFO": "[info]  "}.get(sev, f"[{sev}]")
        lines.append("")
        wrapped = _wrap_text(f"  {prefix} {r['finding']}", width=76, indent="           ")
        lines.append(wrapped)
