# Metrics Reference

Complete reference for every metric computed by odh-eng-metrics. All metrics are
produced by `metrics/calculator.py:compute_all()` and returned as a nested dict.
The same structure is cached in the `metrics_cache` table and exposed via the
`report`, `export-context`, and `serve` commands.

## Accessing Metrics

```bash
# Terminal text summary
make report

# Full JSON output (all metrics)
python3 cli.py report --json-output

# Structured JSON for AI agents (all or per-PR)
python3 cli.py export-context -o data/context.json
python3 cli.py export-context --pr 3346 -o data/pr-context.json

# Prometheus endpoint (for Grafana)
make serve  # → http://localhost:9090/metrics

# Programmatic access
from store.db import Store
from metrics.calculator import compute_all
store = Store("data/eng-metrics.sqlite")
result = compute_all(store)
```

## Top-Level Structure

```python
result = compute_all(store)
# result keys:
{
    "deployment_frequency": {...},
    "lead_time": {...},
    "change_failure_rate": {...},
    "mttr": {...},
    "per_release": [...],
    "throughput": {...},
    "failure_analysis": {...},
    "pr_flow": {...},
    "pipeline_velocity": [...],
    "ai_adoption": {...},
    "ci_efficiency": {...},
    "git_ci_insights": {...},
    "summary": {
        "deployment_frequency": "Elite|High|Medium|Low",
        "lead_time": "See PR cycle time percentiles",
        "change_failure_rate": "Elite|High|Medium|Low",
        "mttr": "Elite|High|Medium|Low",
    },
}
```

---

## DORA Metrics

### Deployment Frequency

**Module:** `metrics/deployment_frequency.py`
**Key:** `result["deployment_frequency"]`
**Source data:** `releases`, `merged_prs`, `downstream_branches` tables

| Field | Type | Description |
|-------|------|-------------|
| `releases.total` | int | Non-EA release count |
| `releases.ea_total` | int | EA release count |
| `releases.by_month` | dict[str, int] | YYYY-MM → release count |
| `releases.avg_gap_days` | float or None | Average days between stable releases |
| `releases.dora_classification` | str | Elite/High/Medium/Low/Insufficient data |
| `pr_merges.total` | int | PRs merged to main |
| `pr_merges.by_month` | dict[str, int] | YYYY-MM → merge count |
| `pr_merges.avg_gap_days` | float or None | Average days between PR merges |
| `pr_merges.dora_classification` | str | DORA classification |
| `downstream_branches.total` | int | Total downstream branches |
| `downstream_branches.ea_count` | int | EA branches |
| `downstream_branches.branches` | list[str] | Branch names |

### Lead Time for Changes

**Module:** `metrics/lead_time.py`
**Key:** `result["lead_time"]`
**Source data:** `merged_prs`, `branch_arrivals`, `jira_issues` tables

Each stage provides: `count`, `mean`, `p50`, `p75`, `p90` (all in hours)

| Field | Description |
|-------|-------------|
| `pr_cycle_time_hours` | First commit → merge (author date → committer date) |
| `pr_review_time_hours` | Created → merge time |
| `to_stable_hours` | Merge → arrival at `stable` branch |
| `to_rhoai_hours` | Merge → arrival at `rhoai` branch |
| `to_release_hours` | Merge → first release tag containing the commit |
| `jira_issue_to_merge_hours` | JIRA created → PR merge, with `by_type` breakdown |

### Change Failure Rate

**Module:** `metrics/change_failure_rate.py`
**Key:** `result["change_failure_rate"]`
**Source data:** `releases`, `merged_prs`, `reverts`, `cherry_picks` tables

| Field | Type | Description |
|-------|------|-------------|
| `total_changes` | int | PR count in period |
| `total_stable_releases` | int | Non-EA release count |
| `patch_releases` | int | Count of patch releases (vX.Y.N where N>0) |
| `patch_release_list` | list[str] | Patch tag names |
| `reverts_on_main` | int | Revert commit count |
| `revert_list` | list[dict] | sha, date, message for each revert |
| `human_cherry_picks` | int | Non-bot cherry-pick count |
| `cherry_pick_branches` | int | Distinct target branches |
| `cherry_pick_list` | list[dict] | pr, branch, title |
| `total_failure_events` | int | patches + reverts + cherry-picks |
| `rate` | float or None | failure_events / total_changes |
| `rate_pct` | str | Formatted percentage |
| `dora_classification` | str | Elite/High/Medium/Low |

### Mean Time to Recovery

**Module:** `metrics/mttr.py`
**Key:** `result["mttr"]`
**Source data:** `releases` table

| Field | Type | Description |
|-------|------|-------------|
| `patch_release_turnaround_hours.count` | int | Patch releases with measurable turnaround |
| `patch_release_turnaround_hours.details` | list[dict] | Each patch with `hours` (human-readable) |
| `patch_release_turnaround_hours.mean` | float or None | Average hours .0 → patch |
| `patch_release_turnaround_hours.p50` | float or None | Median |
| `patch_release_turnaround_hours.p90` | float or None | 90th percentile |
| `overall_recovery_hours.dora_classification` | str | Based on median patch turnaround |

---

## CI Metrics

### CI Efficiency

**Module:** `metrics/ci_efficiency.py`
**Key:** `result["ci_efficiency"]`
**Source data:** `ci_builds` table

| Field | Type | Description |
|-------|------|-------------|
| `available` | bool | Whether CI data exists |
| `total_prs_with_ci` | int | PRs that have CI builds |
| `total_cycles` | int | CI test cycles (grouped by PR + approximate time) |
| `total_job_runs` | int | Individual job executions |
| `first_pass_success_rate` | float or None | Fraction of PRs passing first cycle |
| `first_pass_success_pct` | str | Formatted percentage |
| `retest_tax` | float or None | Avg extra cycles beyond first |
| `cycle_failure_rate` | float or None | Fraction of cycles that fail |
| `cycle_failure_pct` | str | Formatted percentage |
| `cycle_duration_minutes` | dict | count, mean, p50, p90 |
| `ci_hours_per_pr` | dict | count, mean, p50, p90 |
| `cycles_per_pr_distribution` | list[dict] | bucket, count histogram |
| `monthly` | list[dict] | Per-month: cycles, prs, failures, failure_pct, retest_tax |
| `weekly_failures` | list[dict] | Per-week: total, failures |
| `weekly_job_failures` | list[dict] | Per-week per-job: week, job, failures |

### Git-CI Insights

**Module:** `metrics/git_ci_insights.py`
**Key:** `result["git_ci_insights"]`
**Source data:** `ci_builds`, `merged_prs`, `ci_test_results`, `jira_issues`, `code_risk_scores` tables

This is the richest analytics module, correlating CI outcomes with git changes.

| Field | Type | Description |
|-------|------|-------------|
| `component_health` | list[dict] | Per-component CI stats (first_pass_rate, retest_tax, etc.) |
| `code_hotspots` | dict | Risk band → CI failure correlation |
| `component_resource_cost` | list[dict] | Per-component CPU/memory hours |
| `ai_summary` | dict | AI-assisted PRs CI performance vs overall |
| `jira_health` | list[dict] | Top 20 JIRA issues by CI impact |
| `jira_type_health` | list[dict] | CI stats grouped by JIRA issue type |
| `jira_priority_health` | list[dict] | CI stats grouped by JIRA priority |
| `release_health` | list[dict] | CI stats per release tag |
| `revert_signals` | dict | Whether CI warned before reverts occurred |
| `step_breakdown` | list[dict] | CI step duration breakdown |
| `cycle_duration_breakdown` | list[dict] | Duration distribution |
| `infra_vs_code` | dict | Infrastructure vs code failure split |
| `failure_reasons` | list[dict] | Top failure messages |
| `weekly_component_failures` | list[dict] | Per-week per-component failure trends |

---

## Release Metrics

### Per-Release

**Module:** `metrics/per_release.py`
**Key:** `result["per_release"]` (list, not dict)
**Source data:** `releases`, `merged_prs`, `branch_arrivals`, `cherry_picks` tables

Each element represents one release:

| Field | Type | Description |
|-------|------|-------------|
| `tag` | str | Release tag |
| `label` | str | Human-readable version |
| `published` | str | Release date |
| `is_ea` | bool | Whether it's an EA release |
| `pr_count` | int | PRs included in this release |
| `days_since_previous` | float or None | Days since previous release |
| `lead_time_p50/p90/mean` | float or None | Merge → publish hours |
| `cycle_time_p50/p90/mean` | float or None | First commit → merge hours |
| `cherry_picks` | int | Cherry-picks on downstream branch |
| `has_patch` | bool | Whether a patch release followed |
| `patch_turnaround_hours` | float or None | Time to first patch |

### Pipeline Velocity

**Module:** `metrics/pipeline_velocity.py`
**Key:** `result["pipeline_velocity"]` (list, not dict)
**Source data:** `releases`, `merged_prs`, `branch_arrivals`, `downstream_branches` tables

Each element:

| Field | Type | Description |
|-------|------|-------------|
| `tag` | str | Release tag |
| `published` | str | Release date |
| `accumulation_days` | float | Earliest merge in release → tag publish |
| `downstream_days` | float or None | Tag → downstream branch first commit |

### Throughput Over Time

**Module:** `metrics/throughput_over_time.py`
**Key:** `result["throughput"]`
**Source data:** `releases`, `merged_prs`, `cherry_picks`, `reverts` tables

```python
{
    "months": [
        {
            "month": "2025-01",
            "prs_merged": 42,
            "releases_stable": 1,
            "releases_ea": 0,
            "releases_patch": 0,
            "cherry_picks": 3,
            "reverts": 0,
        },
        ...
    ]
}
```

---

## Failure Analysis Metrics

### Failure Analysis

**Module:** `metrics/failure_analysis.py`
**Key:** `result["failure_analysis"]`
**Source data:** `cherry_picks`, `reverts` tables

| Field | Type | Description |
|-------|------|-------------|
| `cherry_picks_by_branch` | list[dict] | branch, count (sorted by count desc) |
| `monthly_failures` | list[dict] | month, cherry_picks, reverts |
| `revert_details` | list[dict] | date, message (truncated) |

### PR Flow

**Module:** `metrics/pr_flow.py`
**Key:** `result["pr_flow"]`
**Source data:** `merged_prs`, `branch_arrivals` tables

| Field | Type | Description |
|-------|------|-------------|
| `time_to_release` | list[dict] | Histogram buckets: <1d, 1-3d, 3-7d, 1-2w, 2-4w, >30d |
| `cycle_time` | list[dict] | Histogram buckets: <1h, 1-4h, 4-24h, 1-3d, 3-7d, 1-2w, >2w |

---

## AI Metrics

### AI Adoption

**Module:** `metrics/ai_adoption.py`
**Key:** `result["ai_adoption"]`
**Source data:** `ai_assisted_commits`, `merged_prs` tables

| Field | Type | Description |
|-------|------|-------------|
| `total_ai_commits` | int | Unique commit SHAs with AI markers |
| `total_commits` | int | Total merged PRs (proxy for total commits) |
| `overall_pct` | float | AI commits as percentage of total |
| `by_tool` | list[dict] | tool, count — breakdown by tool name |
| `months` | list[dict] | month, ai_commits, total_prs, ai_pct, by_tool |

---

## JIRA Metrics

JIRA analytics are computed separately from `compute_all()` and are accessed
via the `jira-report` command or programmatically.

### Base Analytics

**Module:** `metrics/jira_analytics.py`
**Function:** `compute_base_analytics(issues) -> dict`

| Field | Type | Description |
|-------|------|-------------|
| `total` | int | Issue count |
| `status_distribution` | list[dict] | name, count, pct |
| `status_category_distribution` | list[dict] | name, count, pct |
| `type_distribution` | list[dict] | name, count, pct |
| `priority_distribution` | list[dict] | name, count, pct |
| `assignee_distribution` | list[dict] | name, count, pct |
| `component_distribution` | list[dict] | name, count, pct |
| `resolution_rate` | float | Fraction of issues resolved |
| `resolution_time_hours` | dict | count, mean, p50, p90 |
| `open_issue_aging_days` | dict | count, max, p50, p90 |
| `weekly_throughput` | list[dict] | week, resolved |

### Collection Analytics

**Function:** `compute_collection_analytics(issues, collection_cfg) -> dict`

Same as base analytics plus:
- `specialized` — output from the collection's analyzer (e.g., bug-bash funnel)
- `analyzer` — analyzer name string

### Bug Bash Intelligence

**Function:** `compute_bug_bash_intelligence(issues, store, collection_cfg) -> dict`

Cross-references JIRA issues with PR, CI, revert, and code risk data:

| Field | Type | Description |
|-------|------|-------------|
| `available` | bool | Whether analysis could be performed |
| `linked_prs` | dict | PR linkage statistics |
| `nonfixable_analysis` | dict | Root causes for non-fixable issues |
| `acceleration_gap` | dict | What separates accelerated-fix from fully-automated |
| `ci_impact` | dict | CI stats for bug-bash PRs vs baseline |
| `quality_signals` | dict | Revert rate, failure rate for bug-bash PRs |
| `temporal` | dict | Timeline analysis (when issues were triaged/fixed) |
| `recommendations` | list[str] | Actionable improvement suggestions |

---

## Prometheus Metrics

The `exporter/prometheus_exporter.py` exposes metrics at `http://localhost:9090/metrics`
for Grafana consumption. It also serves JSON tables at `/api/tables/*`.

Key Prometheus metrics:

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `eng_deployment_frequency_releases` | Gauge | — | Total releases |
| `eng_deployment_frequency_pr_merges` | Gauge | — | Total PR merges |
| `eng_lead_time_pr_cycle_p50_hours` | Gauge | — | PR cycle time median |
| `eng_lead_time_to_release_p50_hours` | Gauge | — | Merge to release median |
| `eng_change_failure_rate` | Gauge | — | CFR as decimal |
| `eng_mttr_recovery_p50_hours` | Gauge | — | MTTR median |
| `eng_ci_first_pass_rate` | Gauge | — | CI first-pass success rate |
| `eng_ci_retest_tax` | Gauge | — | CI retest overhead |
| `eng_ci_cycle_duration_p50_minutes` | Gauge | — | CI cycle time median |
| `eng_per_release_*` | Gauge | `release` | Per-release metrics |
| `eng_throughput_*` | Gauge | `month` | Monthly throughput |
| `eng_component_*` | Gauge | `component` | Component health |

---

## Data Freshness

- **Git data:** Updated every `make collect` (fetches latest commits)
- **CI data:** Depends on scraper lag; typically 5-15 minutes behind real-time
- **JIRA data:** Collection issues re-fetched if older than 4 hours (configurable)
- **Computed metrics:** Recomputed on every `report` / `export-context` call
- **metrics_cache:** Stores last `compute_all()` result; used by Prometheus exporter
