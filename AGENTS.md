# odh-eng-metrics — Agent Guidelines

Engineering intelligence tool for the
[opendatahub-operator](https://github.com/opendatahub-io/opendatahub-operator).
Collects data from Git, GitHub, CI (VictoriaMetrics/VictoriaLogs), JIRA, and
GCS artifacts, stores everything in SQLite, and produces DORA metrics, CI
failure analysis, regression detection, JIRA analytics, and AI-agent exports.

## Required Reading

Before working on this project, read these documents:

- @README.md — project overview, quick start, DORA metric definitions
- @docs/architecture.md — data flow, collector pipeline, DB schema, metrics computation
- @docs/setup.md — prerequisites, environment variables, CI observability stack
- @docs/collectors.md — each collector in detail, external APIs, and how to add new ones
- @docs/metrics-reference.md — every metric available, where it comes from, how to access it

## Project Structure

```
cli.py                    # Click CLI — all user-facing commands
config.yaml               # Repository URLs, JIRA, CI Observability, collection settings
Makefile                   # Build/collect/report/dashboard targets
requirements.txt           # Python dependencies

collector/                 # Data collection modules
  repo_manager.py          #   Git clone/fetch management
  tag_collector.py         #   Release tags + downstream branches
  pr_collector.py          #   Merged PRs from git log
  revert_detector.py       #   Revert commits on main
  cherry_pick_detector.py  #   Cherry-picks on downstream branches
  branch_tracker.py        #   PR propagation tracking
  ai_commit_detector.py    #   AI-assisted commit detection
  ci_collector.py          #   VictoriaMetrics + VictoriaLogs + GCS
  jira_collector.py        #   JIRA REST API (Cloud v3 + Server v2)
  code_analyzer.py         #   Function-level code risk scoring
  github_client.py         #   GitHub Releases API helper

store/
  db.py                    # SQLite schema, migrations, Store API

metrics/                   # Analytics computation
  calculator.py            #   Orchestrates all metric computation
  deployment_frequency.py  #   Release/PR cadence (DORA)
  lead_time.py             #   PR cycle/review times (DORA)
  change_failure_rate.py   #   CFR from patches/reverts (DORA)
  mttr.py                  #   Mean Time To Recovery (DORA)
  ci_efficiency.py         #   CI cycles, first-pass rate, retest tax
  git_ci_insights.py       #   Component-level CI correlation
  jira_analytics.py        #   Collection analytics + bug-bash intelligence
  per_release.py           #   Per-release metrics
  throughput_over_time.py  #   Monthly trends
  failure_analysis.py      #   Cherry-pick/revert trends
  pr_flow.py               #   PR cycle time histograms
  pipeline_velocity.py     #   Accumulation days, downstream delay
  ai_adoption.py           #   AI tool adoption trends

reports/                   # Report generators
  ci_health_report.py     #   HTML CI report with charts (week/month/3mo)
  failure_patterns.py      #   Codebase-wide failure analysis + regression detection
  failure_investigation.py #   Per-PR investigation
  weekly_digest.py         #   Week-over-week CI digest
  jira_report.py           #   JIRA collection analytics
  json_export.py           #   Structured JSON for AI agents
  assertion_parser.py      #   Go test failure message parser
  links.py                 #   URL builder (Prow, GitHub, Grafana, GCS)

exporter/
  prometheus_exporter.py   # Prometheus /metrics + /api/tables/* JSON

dashboard/                 # Local Grafana + Prometheus stack
  docker-compose.yml
  grafana/dashboards/      #   Dashboard JSON files
  grafana/provisioning/    #   Datasource and dashboard provisioning

data/                      # Runtime data (gitignored)
  repos/*.git              #   Bare clones
  eng-metrics.sqlite       #   SQLite database
```

## Essential Commands

```bash
make collect           # Full pipeline: clone repos, collect all data
make report            # Print DORA + extended metrics
make failure-patterns  # Recurring CI failure analysis (DAYS=30)
make digest            # Weekly CI health digest (WEEKS=1)
make investigate       # Per-PR investigation (PR=<number>)
make export-context    # JSON for AI agents (PR=<n>, DAYS=<n>, OUTPUT=<file>)
make jira-report       # JIRA collection analytics (COLLECTION=<name>, JSON=1)
make ci-report         # HTML CI health report (week/month/3mo) → data/ci-health-report.html
make bug-bash-report   # HTML deep analysis for AI Bug Bash → data/bug-bash-deep-analysis.html
make serve             # Prometheus exporter on :9090
make dashboard         # Start Grafana (:3001) + Prometheus (:9091)
make dashboard-down    # Stop dashboard stack
make refresh           # Collect + restart exporter
make clean             # Wipe data/
```

## Environment Variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `GITHUB_TOKEN` | Optional | Higher GitHub API rate limit for release prerelease flags |
| `JIRA_TOKEN` | For JIRA | JIRA REST API authentication |
| `JIRA_EMAIL` | For JIRA Cloud | Required with token on `*.atlassian.net` |
| `CI_OBS_DIR` | Optional | Path to `openshift-ci-observability` checkout (default: `~/git/openshift-ci-observability`) |

## Key Patterns

### Collector contract

Every collector module exposes a `collect_*()` function that:
1. Takes a `Store` instance and config/repo arguments
2. Queries an external source (Git, API, VictoriaMetrics, etc.)
3. Calls `store.upsert_*()` to persist results
4. Returns an `int` count of items collected
5. Is called from `cli.py:collect()` in a fixed pipeline order

### Adding a new collector

See @docs/collectors.md for the full guide. Summary:
1. Create `collector/<name>.py` with a `collect_<thing>(store, cfg, ...) -> int` function
2. Add any new tables to `store/db.py` SCHEMA + upsert/get methods
3. Wire it into `cli.py:collect()` in the appropriate pipeline position
4. Add config to `config.yaml` if the collector needs settings
5. Add a metrics module in `metrics/` if the data supports computed analytics

### Data storage

All data lives in SQLite (`data/eng-metrics.sqlite`). The `Store` class handles
schema creation, migrations, and provides typed upsert/get methods. JSON columns
(like `jira_keys`, `labels`, `components`) store serialized arrays/objects.

### CI Observability dependency

The CI collector requires the `openshift-ci-observability` stack (VictoriaMetrics
+ VictoriaLogs + scraper). The Makefile's `ensure-ci-obs` target auto-starts it.
Without it, CI data collection is skipped gracefully.
