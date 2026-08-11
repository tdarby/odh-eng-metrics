# Architecture

## Overview

odh-eng-metrics follows a pipeline architecture: **Collect → Store → Compute → Report**.

```
┌──────────────────────────────────────────────────────────────────────┐
│                          DATA SOURCES                                │
│                                                                      │
│  Git repos     GitHub API    VictoriaMetrics   VictoriaLogs   JIRA  │
│  (bare clones) (releases)    (CI builds)       (CI logs)      (REST)│
│       │             │              │                │            │    │
└───────┼─────────────┼──────────────┼────────────────┼────────────┼───┘
        │             │              │                │            │
        ▼             ▼              ▼                ▼            ▼
┌──────────────────────────────────────────────────────────────────────┐
│                        COLLECTORS (collector/)                        │
│                                                                      │
│  repo_manager → tag_collector → pr_collector → revert_detector       │
│  → cherry_pick_detector → branch_tracker → ai_commit_detector        │
│  → ci_collector → code_analyzer → jira_collector                     │
│                                                                      │
│  Each collector: query source → transform → store.upsert_*()         │
└──────────────────────────────┬───────────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│                    SQLite (data/eng-metrics.sqlite)                   │
│                                                                      │
│  releases │ merged_prs │ reverts │ cherry_picks │ downstream_branches│
│  branch_arrivals │ ai_assisted_commits │ ci_builds │ ci_build_steps  │
│  ci_build_failure_messages │ ci_test_results │ jira_issues           │
│  jira_collection_issues │ code_risk_scores │ metrics_cache           │
└──────────────────────────────┬───────────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│                     METRICS / ANALYTICS (metrics/)                    │
│                                                                      │
│  calculator.py orchestrates: deployment_frequency, lead_time,        │
│  change_failure_rate, mttr, ci_efficiency, git_ci_insights,          │
│  per_release, throughput_over_time, failure_analysis, pr_flow,       │
│  pipeline_velocity, ai_adoption, jira_analytics                      │
│                                                                      │
│  Reads from Store → computes derived metrics → caches in metrics_cache│
└──────────────────────────────┬───────────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│                      OUTPUTS (reports/ + exporter/)                   │
│                                                                      │
│  Terminal text │ Markdown │ JSON │ Prometheus │ Grafana dashboards   │
│                                                                      │
│  ci_health_report.py    — HTML report with charts (week/month/3mo)   │
│  failure_patterns.py    — codebase-wide analysis + regression onset  │
│  weekly_digest.py       — week-over-week CI health                   │
│  failure_investigation.py — per-PR deep dive                         │
│  jira_report.py         — JIRA collection analytics                  │
│  json_export.py         — structured JSON for AI agents              │
│  prometheus_exporter.py — /metrics endpoint for Grafana              │
└──────────────────────────────────────────────────────────────────────┘
```

## Collection Pipeline

The `collect` command in `cli.py` runs collectors in a fixed order. Each step
depends on data from prior steps (e.g., `branch_tracker` needs PRs and tags
already collected).

| Order | Collector | Depends on | External source |
|-------|-----------|------------|-----------------|
| 1 | `repo_manager.ensure_repos` | — | Git clone/fetch |
| 2 | `tag_collector.collect_upstream_releases` | repos | Git tags + GitHub Releases API |
| 3 | `tag_collector.collect_downstream_branches` | repos | Git branches |
| 4 | `pr_collector.collect_prs_from_git` | repos | Git log |
| 5 | `revert_detector.collect_reverts` | repos | Git log --grep |
| 6 | `cherry_pick_detector.collect_cherry_picks` | repos | Git log --grep |
| 7 | `branch_tracker.track_pr_propagation` | PRs, tags | Git tag --contains |
| 8 | `ai_commit_detector.collect_ai_commits` | repos | Git log --all |
| 9 | `ci_collector.collect_ci_builds` | — | VictoriaMetrics + VictoriaLogs + GCS |
| 10 | `code_analyzer.analyze_code_risk` | repos | hotspots/gocyclo CLI |
| 11 | `jira_collector.collect_pr_issues` | PRs | JIRA REST API |
| 12 | `jira_collector.collect_collection` (per collection) | — | JIRA REST API |

## Database Schema

All tables use `INSERT OR REPLACE` (upsert) semantics — running `collect`
multiple times is safe and idempotent.

### Core Git tables

**`releases`** — upstream release tags

| Column | Type | Description |
|--------|------|-------------|
| `tag` | TEXT PK | Tag name (e.g. `v2.19.0`) |
| `published` | TEXT | ISO 8601 date |
| `prerelease` | INT | 1 if GitHub marks it prerelease |
| `is_patch` | INT | 1 if `v*.*.N` where N > 0 |
| `is_ea` | INT | 1 if tag matches EA pattern |

**`merged_prs`** — PRs merged to upstream main

| Column | Type | Description |
|--------|------|-------------|
| `repo` | TEXT | `owner/repo` |
| `number` | INT | PR number |
| `title` | TEXT | PR title |
| `author` | TEXT | Commit author |
| `created_at` | TEXT | Author date of first commit |
| `merged_at` | TEXT | Committer date (merge time) |
| `first_commit_at` | TEXT | Earliest author date in PR |
| `base_branch` | TEXT | Target branch |
| `additions` | INT | Lines added |
| `deletions` | INT | Lines deleted |
| `jira_keys` | TEXT | JSON array of JIRA keys from commit messages |
| `merge_sha` | TEXT | Merge commit SHA |
| `is_ai_assisted` | INT | 1 if AI tool markers detected |
| `changed_files` | TEXT | JSON array of changed file paths |
| `changed_components` | TEXT | JSON array of inferred component names |

**`reverts`** — revert commits on upstream main

| Column | Type | Description |
|--------|------|-------------|
| `sha` | TEXT PK | Revert commit SHA |
| `date` | TEXT | Commit date |
| `reverted_sha` | TEXT | SHA being reverted |
| `message` | TEXT | Commit message |
| `reverted_pr` | INT | PR number being reverted (if detected) |

**`cherry_picks`** — backport commits on downstream branches

| Column | Type | Description |
|--------|------|-------------|
| `pr_number` | INT | PR number on downstream |
| `target_branch` | TEXT | Branch cherry-picked to |
| `title` | TEXT | Commit title |
| `merged_at` | TEXT | Merge date |

**`downstream_branches`** — tracked downstream release branches

| Column | Type | Description |
|--------|------|-------------|
| `name` | TEXT PK | Branch name (e.g. `rhoai-2.19`) |
| `first_commit_date` | TEXT | When the branch first appeared |
| `is_ea` | INT | 1 if EA branch |

**`branch_arrivals`** — when PRs reach downstream branches/tags

| Column | Type | Description |
|--------|------|-------------|
| `pr_repo` | TEXT | PR's repo |
| `pr_number` | INT | PR number |
| `branch` | TEXT | Branch or `tag:vX.Y.Z` |
| `arrived_at` | TEXT | Date the commit appeared |

**`ai_assisted_commits`** — commits with AI tool markers

| Column | Type | Description |
|--------|------|-------------|
| `sha` | TEXT | Commit SHA |
| `tool` | TEXT | Tool name (cursor, copilot, etc.) |
| `date` | TEXT | Commit date |

### CI tables

**`ci_builds`** — one row per CI job execution

| Column | Type | Description |
|--------|------|-------------|
| `build_id` | TEXT PK | Prow build ID |
| `pr_number` | INT | PR under test |
| `job_name` | TEXT | Full Prow job name |
| `duration_seconds` | REAL | Total wall-clock time |
| `result` | TEXT | success/failure/error/unknown |
| `started_at` | TEXT | ISO 8601 start time |
| `peak_cpu_cores` | REAL | Max CPU usage (from VictoriaMetrics) |
| `peak_memory_bytes` | REAL | Max memory usage |
| `total_step_seconds` | REAL | Sum of all step durations |

**`ci_build_steps`** — per-step breakdown within a build

| Column | Type | Description |
|--------|------|-------------|
| `build_id` | TEXT | FK to ci_builds |
| `step_name` | TEXT | ci-operator step type |
| `duration_seconds` | REAL | Step duration |
| `level` | TEXT | Step nesting level |
| `is_infra` | INT | 1 if infrastructure step |

**`ci_build_failure_messages`** — aggregated failure messages per build

| Column | Type | Description |
|--------|------|-------------|
| `build_id` | TEXT | FK to ci_builds |
| `message` | TEXT | Error message text (truncated to 500 chars) |
| `source` | TEXT | Where the message came from |
| `count` | INT | Occurrence count |

**`ci_test_results`** — individual test outcomes from JUnit XML

| Column | Type | Description |
|--------|------|-------------|
| `build_id` | TEXT | FK to ci_builds |
| `test_name` | TEXT | Fully qualified test name |
| `suite` | TEXT | Test suite name |
| `test_variant` | TEXT | Job variant identifier |
| `status` | TEXT | passed/failed/skipped/error |
| `duration_seconds` | REAL | Test execution time |
| `is_leaf` | INT | 1 if leaf test (not wrapper) |
| `failure_message` | TEXT | Failure message text |

### JIRA tables

**`jira_issues`** — JIRA issue metadata

| Column | Type | Description |
|--------|------|-------------|
| `key` | TEXT PK | Issue key (e.g. `RHOAIENG-1234`) |
| `summary` | TEXT | Issue title |
| `issue_type` | TEXT | Bug, Story, Task, etc. |
| `priority` | TEXT | Critical, Major, etc. |
| `status` | TEXT | Current status |
| `status_category` | TEXT | To Do, In Progress, Done |
| `assignee` | TEXT | Assignee display name |
| `components` | TEXT | Comma-separated component names |
| `labels` | TEXT | Comma-separated labels |
| `fix_versions` | TEXT | Comma-separated fix versions |
| `story_points` | REAL | Story point value |
| `created` | TEXT | Issue creation date |
| `resolved` | TEXT | Resolution date |
| `epic_key` | TEXT | Parent epic key |
| `description` | TEXT | Full issue description (plain text) |
| `comments` | TEXT | JSON array of comment objects |
| `fetched_at` | TEXT | When this row was last refreshed |

**`jira_collection_issues`** — many-to-many between collections and issues

| Column | Type | Description |
|--------|------|-------------|
| `collection_name` | TEXT | Collection identifier |
| `issue_key` | TEXT | FK to jira_issues.key |

### Other tables

**`code_risk_scores`** — function-level complexity analysis

| Column | Type | Description |
|--------|------|-------------|
| `file` | TEXT | Go source file path |
| `function` | TEXT | Function name |
| `component` | TEXT | Inferred component |
| `complexity` | REAL | Cyclomatic complexity |
| `churn_30d` | INT | Git changes in last 30 days |
| `risk_score` | REAL | Composite risk score |
| `risk_band` | TEXT | Critical/High/Medium/Low |

**`metrics_cache`** — cached computed metrics

| Column | Type | Description |
|--------|------|-------------|
| `metric` | TEXT | Metric identifier |
| `window` | TEXT | Time window |
| `value` | TEXT | JSON-serialized result |
| `computed_at` | TEXT | Computation timestamp |

## Metrics Computation Flow

`metrics/calculator.py:compute_all()` orchestrates all analytics:

1. Reads raw data from Store (releases, PRs, reverts, cherry-picks, branches, CI builds, test results, JIRA issues)
2. Passes data through each metric module
3. Returns a nested dict with all computed metrics
4. Optionally caches the result in `metrics_cache`

The `report` and `export-context` commands call `compute_all()` and format the
output. Individual report commands (like `failure-patterns`) may read raw data
directly from Store for more specialized analysis.

## External Service Dependencies

| Service | Protocol | Required? | Purpose |
|---------|----------|-----------|---------|
| Git (upstream/downstream repos) | HTTPS clone | Yes | All git-based collection |
| GitHub Releases API | HTTPS REST | Optional | Prerelease flag on tags |
| VictoriaMetrics | HTTP PromQL | For CI data | Build durations, results, resource metrics |
| VictoriaLogs | HTTP LogsQL | For CI data | Step failure messages, test failure messages |
| GCS (`test-platform-results`) | HTTPS | Fallback | JUnit XML when VictoriaLogs lacks data |
| JIRA REST API | HTTPS | For JIRA data | Issue metadata, comments, collection membership |
| hotspots / gocyclo | Local CLI | Optional | Code complexity scoring |

### CI Observability Stack

The CI collector depends on the `openshift-ci-observability` project, which runs:
- **VictoriaMetrics** (port 8428) — stores CI build metrics scraped from Prow/GCS
- **VictoriaLogs** (port 9428) — stores CI log messages parsed from JUnit XML
- **Scraper** — fetches artifacts from GCS, parses JUnit, pushes to VM/VL
- **Grafana** (port 3000) — dashboards for the CI data

The `ensure-ci-obs` Makefile target checks for running containers and restarts
the stack if any are missing. The scraper needs time to ingest data; on first
run, the CI collector waits up to `ingest_wait` seconds (default 180).
