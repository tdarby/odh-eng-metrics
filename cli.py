#!/usr/bin/env python3
"""Engineering Metrics CLI for opendatahub-operator."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import click
import yaml

from collector import agentready_collector, ai_commit_detector, branch_tracker, cherry_pick_detector, ci_collector, code_analyzer, jira_collector, manifest_tracker, pr_collector, revert_detector, tag_collector
from collector.repo_manager import ensure_repos
from metrics.calculator import compute_all
from store.db import Store

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def _load_config() -> dict:
    cfg_path = Path(__file__).parent / "config.yaml"
    with open(cfg_path) as f:
        return yaml.safe_load(f)


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format=LOG_FORMAT, stream=sys.stderr)
    # httpx logs every HTTP request at INFO; keep the output clean
    logging.getLogger("httpx").setLevel(logging.WARNING)


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging")
def cli(verbose: bool) -> None:
    """Engineering Metrics for opendatahub-operator."""
    _setup_logging(verbose)


@cli.command()
@click.option("--force", is_flag=True, help="Re-collect data even if it already exists")
def collect(force: bool) -> None:
    """Clone/fetch repos and collect all data from git history (+ 1 API call for releases)."""
    cfg = _load_config()
    data_dir = Path(cfg["collection"]["data_dir"])
    store = Store(cfg["collection"]["cache_db"])
    lookback = cfg["collection"]["lookback_days"]

    click.echo("Cloning/fetching repositories...")
    upstream_repo, downstream_repo = ensure_repos(cfg, data_dir)

    upstream_path = Path(upstream_repo.common_dir)
    downstream_path = Path(downstream_repo.common_dir)
    up = cfg["upstream"]
    repo_name = f"{up['owner']}/{up['repo']}"

    click.echo("Collecting upstream releases (git tags + 1 API call for prerelease flags)...")
    n = tag_collector.collect_upstream_releases(store, upstream_repo, cfg)
    click.echo(f"  {n} releases stored")

    click.echo("Collecting downstream release branches (git only)...")
    n = tag_collector.collect_downstream_branches(store, downstream_repo, cfg)
    click.echo(f"  {n} branches stored")

    click.echo("Collecting merged PRs from git log (upstream main)...")
    n = pr_collector.collect_prs_from_git(
        store, upstream_path, repo_name,
        branch="main", lookback_days=lookback,
    )
    click.echo(f"  {n} PRs stored")

    click.echo("Detecting reverts on upstream main (git only)...")
    n = revert_detector.collect_reverts(store, upstream_repo, branch="main", lookback_days=lookback)
    click.echo(f"  {n} reverts found")

    click.echo("Detecting cherry-picks on downstream release branches (git only)...")
    n = cherry_pick_detector.collect_cherry_picks(store, downstream_repo, cfg, lookback_days=lookback)
    click.echo(f"  {n} cherry-picks found")

    click.echo("Tracking commit propagation across branches (git only)...")
    n = branch_tracker.track_pr_propagation(store, upstream_path, cfg)
    click.echo(f"  {n} branch arrivals tracked")

    click.echo("Detecting AI-assisted commits (git only)...")
    n = ai_commit_detector.collect_ai_commits(store, upstream_path, repo_name, lookback_days=lookback)
    ds_name = f"{cfg['downstream']['owner']}/{cfg['downstream']['repo']}"
    n += ai_commit_detector.collect_ai_commits(store, downstream_path, ds_name, lookback_days=lookback)
    click.echo(f"  {n} AI-assisted commit markers found")

    click.echo("Tracking component manifest SHA pins (git only)...")
    n = manifest_tracker.collect_manifest_pins(store, upstream_path)
    click.echo(f"  {n} manifest pin records stored")

    click.echo("Fetching upstream changelogs for manifest SHA deltas (GitHub API)...")
    n = manifest_tracker.collect_manifest_deltas(store)
    if n > 0:
        click.echo(f"  {n} manifest deltas fetched")
    else:
        click.echo("  no new deltas to fetch (or GITHUB_TOKEN not set)")

    click.echo("Collecting CI build data (VictoriaMetrics)...")
    n = ci_collector.collect_ci_builds(store, cfg, lookback_days=lookback)
    if n > 0:
        click.echo(f"  {n} CI builds stored")
    else:
        click.echo("  no CI data (CI Observability stack may not be running)")

    click.echo("Collecting metadata for open/unmerged PRs referenced in CI (GitHub API)...")
    n = pr_collector.collect_open_pr_metadata(store, repo_name, cfg)
    click.echo(f"  {n} open PR metadata records stored")

    click.echo("Analyzing code risk (hotspots/gocyclo)...")
    n = code_analyzer.analyze_code_risk(store, upstream_path, repo_name, force=force)
    if n > 0:
        click.echo(f"  {n} function risk scores stored")
    else:
        click.echo("  no code analysis tool available (install hotspots or gocyclo)")

    if cfg.get("jira", {}).get("enabled"):
        click.echo("Enriching with JIRA issue metadata...")
        n = jira_collector.collect_pr_issues(store, cfg)
        if n > 0:
            click.echo(f"  {n} JIRA issues fetched/updated")
        else:
            click.echo("  no new JIRA issues to fetch (or JIRA_TOKEN not set)")

        collections = cfg.get("jira", {}).get("collections", [])
        for coll in collections:
            selector = (
                coll.get("labels")
                or f"prefix:{coll['label_prefix']}" if coll.get("label_prefix")
                else coll.get("jql", "custom JQL")
            )
            click.echo(f"Collecting JIRA issues for '{coll['name']}' ({selector})...")
            n = jira_collector.collect_collection(store, cfg, coll)
            click.echo(f"  {n} issues in collection")

    for coll in cfg.get("jira", {}).get("collections", []):
        if coll.get("project_repos"):
            click.echo(f"Running readiness assessments for '{coll['name']}'...")
            n = agentready_collector.collect_assessments(store, cfg, collection_name=coll["name"], force=force)
            if n > 0:
                click.echo(f"  {n} repo(s) assessed")
            else:
                click.echo("  no repos to assess (already cached or check project_repos config)")

    store.close()
    click.echo("Collection complete.")


@cli.command()
@click.option("--collection", default="ai-bug-bash", help="Collection name to assess")
@click.option("--force", is_flag=True, help="Re-assess even if data already exists")
def agentready(collection: str, force: bool) -> None:
    """Run AI Bug Automation Readiness assessments on repos mapped to JIRA projects."""
    cfg = _load_config()
    store = Store(cfg["collection"]["cache_db"])
    click.echo(f"Running readiness assessments for '{collection}'...")
    n = agentready_collector.collect_assessments(store, cfg, collection_name=collection, force=force)
    store.close()
    if n:
        click.echo(f"  {n} repo(s) assessed and stored.")
    else:
        click.echo("  No assessments produced (check config project_repos).")


@cli.command()
@click.option("--json-output", is_flag=True, help="Output as JSON instead of text")
def report(json_output: bool) -> None:
    """Compute and display DORA metrics summary."""
    cfg = _load_config()
    store = Store(cfg["collection"]["cache_db"])
    lookback = cfg["collection"]["lookback_days"]
    min_ver = cfg.get("per_release", {}).get("min_version", "3.0.0")
    result = compute_all(store, lookback_days=lookback, min_version=min_ver)
    store.close()

    if json_output:
        click.echo(json.dumps(result, indent=2))
        return

    _print_text_report(result)


def _fmt_hours(h: float | None) -> str:
    """Format hours as a human-friendly string: hours if <48h, days otherwise."""
    if h is None:
        return "N/A"
    if abs(h) < 48:
        return f"{h:.1f}h"
    return f"{h / 24:.1f}d"


def _print_text_report(result: dict) -> None:
    click.echo()
    click.echo("=" * 70)
    click.echo("  Engineering Metrics Report: opendatahub-operator")
    click.echo("=" * 70)

    # Deployment Frequency
    df = result["deployment_frequency"]
    click.echo()
    click.echo("DEPLOYMENT FREQUENCY")
    click.echo("-" * 40)
    rel = df["releases"]
    click.echo(f"  Upstream releases:     {rel['total']} stable + {rel.get('ea_total', 0)} EA")
    if rel["avg_gap_days"] is not None:
        click.echo(f"  Avg gap between:       {rel['avg_gap_days']:.1f} days")
    click.echo(f"  DORA classification:   {rel['dora_classification']}")
    pr_info = df["pr_merges"]
    click.echo(f"  PR merges to main:     {pr_info['total']} total")
    if pr_info["avg_gap_days"] is not None:
        click.echo(f"  Avg gap between PRs:   {pr_info['avg_gap_days']:.2f} days")
    click.echo(f"  PR-level DORA:         {pr_info['dora_classification']}")
    ds = df["downstream_branches"]
    click.echo(f"  Downstream branches:   {ds['total']} ({ds['ea_count']} EA)")

    # Lead Time
    lt = result["lead_time"]
    click.echo()
    click.echo("LEAD TIME FOR CHANGES")
    click.echo("-" * 40)
    for stage, label in [
        ("pr_cycle_time_hours", "PR cycle time (1st commit -> merge)"),
        ("pr_review_time_hours", "PR review time (opened -> merge)"),
        ("to_stable_hours", "Merge -> stable branch"),
        ("to_rhoai_hours", "Merge -> rhoai branch"),
        ("to_release_hours", "Merge -> tagged release"),
    ]:
        data = lt.get(stage, {})
        n = data.get("count", 0)
        if n == 0:
            click.echo(f"  {label}: no data")
            continue
        click.echo(f"  {label}")
        click.echo(
            f"    n={n}  mean={_fmt_hours(data.get('mean'))}  "
            f"p50={_fmt_hours(data.get('p50'))}  p90={_fmt_hours(data.get('p90'))}"
        )

    # Change Failure Rate
    cfr = result["change_failure_rate"]
    click.echo()
    click.echo("CHANGE FAILURE RATE")
    click.echo("-" * 40)
    click.echo(f"  Total changes (PRs):   {cfr['total_changes']}")
    click.echo(f"  Stable releases:       {cfr['total_stable_releases']}")
    click.echo(f"  Patch releases:        {cfr['patch_releases']}  {cfr['patch_release_list']}")
    click.echo(f"  Reverts on main:       {cfr['reverts_on_main']}")
    click.echo(f"  Cherry-pick commits:   {cfr['human_cherry_picks']} across {cfr['cherry_pick_branches']} branches")
    click.echo(f"  Total failure events:  {cfr['total_failure_events']}")
    click.echo(f"  Failure rate:          {cfr['rate_pct']}")
    click.echo(f"  DORA classification:   {cfr['dora_classification']}")

    # MTTR
    mt = result["mttr"]
    click.echo()
    click.echo("MEAN TIME TO RECOVERY")
    click.echo("-" * 40)
    ptr = mt["patch_release_turnaround_hours"]
    if ptr["count"] > 0:
        click.echo("  Patch release turnaround:")
        click.echo(
            f"    n={ptr['count']}  mean={_fmt_hours(ptr.get('mean'))}  "
            f"p50={_fmt_hours(ptr.get('p50'))}  p90={_fmt_hours(ptr.get('p90'))}"
        )
        for d in ptr.get("details", []):
            click.echo(f"      {d['patch']}: {d.get('hours', 'N/A')}")
    else:
        click.echo("  Patch release turnaround: no data")
    ovr = mt["overall_recovery_hours"]
    click.echo(f"  DORA classification:   {ovr['dora_classification']}")

    # Per-Release Breakdown
    per_rel = result.get("per_release", [])
    if per_rel:
        click.echo()
        click.echo("PER-RELEASE BREAKDOWN (v3.x+)")
        click.echo("-" * 90)
        header = (
            f"  {'Release':<20s} {'PRs':>4s} {'Cadence':>8s} {'Lead p50':>9s} "
            f"{'Cycle p50':>10s} {'Cherry':>7s} {'Patch?':>7s}"
        )
        click.echo(header)
        click.echo("  " + "-" * 86)
        for r in per_rel:
            cadence = f"{r['days_since_previous']:.0f}d" if r["days_since_previous"] else "-"
            lead = _fmt_hours(r["lead_time_p50"])
            cycle = _fmt_hours(r["cycle_time_p50"])
            patch = "Yes" if r["has_patch"] else "No"
            click.echo(
                f"  {r['label']:<20s} {r['pr_count']:>4d} {cadence:>8s} {lead:>9s} "
                f"{cycle:>10s} {r['cherry_picks']:>7d} {patch:>7s}"
            )

    # Throughput Over Time
    throughput = result.get("throughput", {})
    months = throughput.get("months", [])
    if months:
        recent = months[-6:] if len(months) >= 6 else months
        click.echo()
        click.echo("THROUGHPUT OVER TIME (recent months)")
        click.echo("-" * 70)
        header = f"  {'Month':<10s} {'PRs':>5s} {'Stable':>7s} {'EA':>4s} {'Patch':>6s} {'Cherry':>7s} {'Reverts':>8s}"
        click.echo(header)
        click.echo("  " + "-" * 66)
        for m in recent:
            click.echo(
                f"  {m['month']:<10s} {m['prs_merged']:>5d} {m['releases_stable']:>7d} "
                f"{m['releases_ea']:>4d} {m['releases_patch']:>6d} "
                f"{m['cherry_picks']:>7d} {m['reverts']:>8d}"
            )

    # PR Flow
    flow = result.get("pr_flow", {})
    ttr = flow.get("time_to_release", [])
    if ttr:
        click.echo()
        click.echo("PR TIME-TO-RELEASE DISTRIBUTION")
        click.echo("-" * 40)
        for b in ttr:
            bar = "#" * (b["count"] // 5) if b["count"] > 0 else ""
            click.echo(f"  {b['bucket']:>8s}: {b['count']:>4d}  {bar}")

    cycle = flow.get("cycle_time", [])
    if any(b["count"] > 0 for b in cycle):
        click.echo()
        click.echo("PR CYCLE TIME DISTRIBUTION")
        click.echo("-" * 40)
        for b in cycle:
            bar = "#" * b["count"] if b["count"] > 0 else ""
            click.echo(f"  {b['bucket']:>8s}: {b['count']:>4d}  {bar}")

    # Pipeline Velocity
    pipeline = result.get("pipeline_velocity", [])
    if pipeline:
        click.echo()
        click.echo("PIPELINE VELOCITY (v3.x+)")
        click.echo("-" * 60)
        header = f"  {'Release':<20s} {'Accumulation':>13s} {'Downstream':>12s}"
        click.echo(header)
        click.echo("  " + "-" * 56)
        for r in pipeline:
            acc = f"{r['accumulation_days']:.0f}d" if r["accumulation_days"] else "-"
            ds = f"{r['downstream_days']:.0f}d" if r["downstream_days"] else "N/A"
            click.echo(f"  {r['label']:<20s} {acc:>13s} {ds:>12s}")

    # AI Adoption
    ai = result.get("ai_adoption", {})
    if ai.get("total_ai_commits", 0) > 0:
        click.echo()
        click.echo("AI ADOPTION (labeled commits, lower bound)")
        click.echo("-" * 60)
        click.echo(f"  Total AI-assisted commits:  {ai['total_ai_commits']}  ({ai['overall_pct']:.1f}% of PRs)")
        click.echo(f"  Tools detected:")
        for t in ai.get("by_tool", []):
            click.echo(f"    {t['tool']:>10s}: {t['count']}")
        ai_months = ai.get("months", [])
        recent_months = ai_months[-8:]
        if recent_months:
            click.echo(f"  Monthly trend (recent):")
            for m in recent_months:
                click.echo(f"    {m['month']}: {m['ai_commits']} AI commits ({m['ai_pct']:.1f}% of {m['total_prs']} PRs)")

    # CI Efficiency
    ci = result.get("ci_efficiency", {})
    if ci.get("available"):
        click.echo()
        click.echo("CI EFFICIENCY (from CI Observability)")
        click.echo("-" * 60)
        click.echo(f"  PRs with CI data:       {ci['total_prs_with_ci']}")
        click.echo(f"  Test cycles:            {ci['total_cycles']}  ({ci['total_job_runs']} individual job runs)")
        click.echo(f"  First-pass success:     {ci['first_pass_success_pct']}")
        click.echo(f"  Retest tax (cycles/PR): {ci['retest_tax']}")
        click.echo(f"  Cycle failure rate:     {ci['cycle_failure_pct']}")

        dur = ci.get("cycle_duration_minutes", {})
        if dur.get("count", 0) > 0:
            click.echo(f"  Cycle duration:         "
                        f"mean={dur['mean']:.0f}m  p50={dur['p50']:.0f}m  p90={dur['p90']:.0f}m")

        ci_hrs = ci.get("ci_hours_per_pr", {})
        if ci_hrs.get("count", 0) > 0:
            click.echo(f"  CI wait per PR:         "
                        f"mean={_fmt_hours(ci_hrs['mean'])}  p50={_fmt_hours(ci_hrs['p50'])}  "
                        f"p90={_fmt_hours(ci_hrs['p90'])}")

        cpp = ci.get("cycles_per_pr_distribution", [])
        if cpp:
            click.echo(f"  Cycles-per-PR distribution:")
            for b in cpp:
                bar = "#" * (b["count"] // 2) if b["count"] > 0 else ""
                click.echo(f"    {b['bucket']:>8s}: {b['count']:>4d}  {bar}")

        ci_months = ci.get("monthly", [])
        if ci_months:
            recent = ci_months[-6:] if len(ci_months) >= 6 else ci_months
            click.echo(f"  Monthly CI health:")
            header = f"    {'Month':<10s} {'Cycles':>7s} {'PRs':>5s} {'Fail%':>6s} {'Tax':>5s}"
            click.echo(header)
            click.echo("    " + "-" * 40)
            for m in recent:
                click.echo(
                    f"    {m['month']:<10s} {m['cycles']:>7d} {m['prs']:>5d} "
                    f"{m['failure_pct']:>5.1f}% {m['retest_tax']:>5.2f}"
                )

    click.echo()
    click.echo("=" * 70)
    click.echo("  SUMMARY")
    click.echo("=" * 70)
    for metric, value in result["summary"].items():
        click.echo(f"  {metric:30s} {value}")
    click.echo()


@cli.command()
@click.option("--pr", type=int, default=None, help="PR number (default: most recently failed PR)")
@click.option("--json-output", is_flag=True, help="Output as JSON instead of markdown")
def investigate(pr: int | None, json_output: bool) -> None:
    """Generate a failure investigation report for a PR."""
    from reports import failure_investigation, json_export, links

    cfg = _load_config()
    store = Store(cfg["collection"]["cache_db"])
    lb = links.from_config(cfg)

    if json_output:
        if pr is None:
            click.echo("Error: --pr is required with --json-output", err=True)
            store.close()
            sys.exit(1)
        result = json_export.export_pr_context(store, pr, links=lb)
        click.echo(json.dumps(result, indent=2))
    else:
        click.echo(failure_investigation.generate(store, pr_number=pr, links=lb))

    store.close()


@cli.command()
@click.option("--weeks", type=int, default=1, help="Number of weeks to cover (default: 1)")
def digest(weeks: int) -> None:
    """Generate a weekly CI health digest."""
    from reports import weekly_digest, links

    cfg = _load_config()
    store = Store(cfg["collection"]["cache_db"])
    lb = links.from_config(cfg)
    click.echo(weekly_digest.generate(store, weeks_back=weeks, links=lb))
    store.close()


@cli.command("failure-patterns")
@click.option("--days", type=int, default=30, help="Lookback period in days (default: 30)")
def failure_patterns(days: int) -> None:
    """Analyze recurring failure patterns across CI builds."""
    from reports import failure_patterns as fp, links

    cfg = _load_config()
    store = Store(cfg["collection"]["cache_db"])
    lb = links.from_config(cfg)
    click.echo(fp.generate(store, lookback_days=days, links=lb))
    store.close()


@cli.command("export-context")
@click.option("--pr", type=int, default=None, help="PR number for per-PR context")
@click.option("--days", type=int, default=30, help="Lookback period for codebase health (default: 30)")
@click.option("-o", "--output", type=click.Path(), default=None, help="Write to file instead of stdout")
def export_context(pr: int | None, days: int, output: str | None) -> None:
    """Export structured JSON context for AI agents.

    With --pr: exports detailed context for a single PR.
    Without --pr: exports codebase-wide CI health summary.
    """
    from reports import json_export, links

    cfg = _load_config()
    store = Store(cfg["collection"]["cache_db"])
    lb = links.from_config(cfg)

    if pr is not None:
        result = json_export.export_pr_context(store, pr, links=lb)
    else:
        result = json_export.export_codebase_health(store, lookback_days=days, links=lb)

    store.close()

    text = json.dumps(result, indent=2)
    if output:
        Path(output).write_text(text)
        click.echo(f"Exported to {output}")
    else:
        click.echo(text)


@cli.command("jira-report")
@click.argument("collection")
@click.option("--json-output", is_flag=True, help="Output as JSON instead of text")
def jira_report(collection: str, json_output: bool) -> None:
    """Analyze a JIRA issue collection (e.g. bug bash results)."""
    from reports import jira_report as jr

    cfg = _load_config()
    store = Store(cfg["collection"]["cache_db"])

    coll_cfg = None
    for c in cfg.get("jira", {}).get("collections", []):
        if c["name"] == collection:
            coll_cfg = c
            break

    issues = store.get_collection_issues(collection)
    if not issues:
        click.echo(f"No issues found for collection '{collection}'. Run 'make collect' first.", err=True)
        store.close()
        sys.exit(1)

    result = jr.generate(issues, collection_name=collection, collection_cfg=coll_cfg, store=store)
    store.close()

    if json_output:
        click.echo(json.dumps(result, indent=2))
    else:
        click.echo(jr.render_text(result))


@cli.command("ci-report")
@click.option("-o", "--output", type=click.Path(), default="data/ci-health-report.html",
              help="Output path for the HTML report")
def ci_report(output: str) -> None:
    """Generate an HTML CI health report with charts (week / month / 3 months)."""
    from reports import ci_health_report

    cfg = _load_config()
    store = Store(cfg["collection"]["cache_db"])

    click.echo("Generating CI health report...")
    path = ci_health_report.generate(store, output_path=output)
    store.close()
    click.echo(f"Report written to {path}")


@cli.command()
@click.option("--port", default=9090, help="Port for the metrics server")
def serve(port: int) -> None:
    """Start Prometheus metrics exporter."""
    from exporter.prometheus_exporter import start_server
    cfg = _load_config()
    start_server(cfg, port)


if __name__ == "__main__":
    cli()
