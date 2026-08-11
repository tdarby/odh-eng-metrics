# ODH Engineering Metrics

Engineering metrics and intelligence for the
[opendatahub-operator](https://github.com/opendatahub-io/opendatahub-operator) project.
Tracks the full delivery pipeline from PR merge through downstream release,
combining [DORA metrics](https://dora.dev/guides/dora-metrics-four-keys/),
CI failure analysis, regression detection, and AI agent context exports.

Started as a DORA metrics collector, evolved through iteration into a broader
engineering intelligence tool that correlates git changes with CI outcomes to
produce actionable insights for both humans and AI agents.

## What's included

- **DORA metrics** — Deployment Frequency, Lead Time, Change Failure Rate, MTTR
- **CI efficiency** — First-pass success rate, retest tax, cycle duration, per-component health
- **JIRA enrichment** — Issue metadata (type, priority, status) for CI correlation and standalone collection analytics
- **JIRA collections** — Label-based issue sets (e.g. bug bash events) with lifecycle, funnel, and distribution analysis
- **Regression onset detection** — Identifies when a test started failing and which PR likely caused it
- **Assertion parsing** — Extracts structured errors from Go test failures, cutting through framework noise
- **Failure pattern analysis** — Broken/flaky test classification, infrastructure vs code separation
- **AI agent exports** — Structured JSON context and markdown reports for LLM-based agents
- **Reports** — Weekly digest, per-PR investigation, failure patterns, JIRA collection analysis

## Reports

### Failure Patterns (`failure-patterns`)

The main report. Analyzes recurring CI failures across all recent builds:

- **Broken tests** (>80% fail rate) with parsed error messages, test file paths, and investigation links
- **Regression onset** — for each broken test, identifies the likely causal PR:
  - *PR under test*: one PR dominates failures (its own code is wrong)
  - *Merged to main*: a recently-merged PR broke the test for everyone
  - Candidates ranked by relevance (component overlap, code vs docs changes) not just recency
- **Flaky tests** (20-80% fail rate) with error summaries
- **Infrastructure failures** separated from code bugs, with wasted CI hours quantified
- **Manifest-induced regressions** — detects when automated image bumps cause failure spikes
- **Prioritized recommendations** — each with "Start with PR#X" pointing to the likely cause

### Weekly Digest (`digest`)

Week-over-week CI health summary:

- **Codebase-wide breakages** — tests broken across all PRs with causal merged PR identification
- **PR-specific failures** — tests failing only in specific PRs (the author needs to fix their code)
- **Resolved this week** — tests that were broken last week but are now passing
- **Still broken (ongoing)** — persistent codebase-wide failures
- **Component health** — risk levels based on component-specific test failures (not inherited infra noise)
- Infrastructure vs code narrative, CI duration trends, AI-assisted commit tracking

### Failure Investigation (`investigate`)

Per-PR deep-dive: build history, step failures, error messages, and how the PR's
failure patterns compare to the broader codebase.

### JIRA Collection Report (`jira-report`)

Standalone analytics for label-based JIRA issue collections. Designed for
event-based analysis (e.g. bug bash results) independent of CI data:

- **Lifecycle**: resolution rate, resolution time (p50/p90), open issue aging
- **Distributions**: status, type, priority, assignee, component breakdowns
- **Throughput**: weekly resolution trend
- **Specialized analyzers**: bug bash funnel analysis (label progression, conversion rates, severity profile)

### CI Health Report (`ci-report`)

Self-contained HTML report with embedded charts comparing CI health across
three time periods: last working week, last month, and last 3 months. Includes:

- **Executive summary** with KPIs per period (first-pass rate, failure rate, retest tax, wasted hours)
- **Weekly trend charts** — pass/fail volume, failure rate line, failures by job type
- **Failure analysis** — infrastructure vs code failure split (donut charts per period)
- **Test health** — broken (>80% fail) and flaky (20-80%) test lists with failure rates
- **Component health** — per-component cycle failure rates
- **CI duration** — mean/p50/p90 cycle times compared across periods
- **Top failing steps** — most common CI step errors

### JSON Context Export (`export-context`)

Structured JSON for programmatic consumption by AI agents. Per-PR or codebase-wide.

## Delivery pipeline

```
opendatahub-operator/main  →(fast-forward)→  stable  →(fast-forward)→  rhoai
                                                                          ↓
                                                            rhods-operator/main → rhoai-x.y branches
                                                                                   ↓
                                                                              Konflux builds → production
```

Actual deployments to production happen outside both repositories (via Konflux),
so we use proxy signals to approximate delivery events.

## Quick start

```bash
# Install dependencies
pip install -r requirements.txt

# Optional: set a GitHub token for the prerelease flag API call
export GITHUB_TOKEN=ghp_...

# Optional: set JIRA credentials for issue metadata enrichment
# Atlassian Cloud (*.atlassian.net) requires both email + API token
export JIRA_EMAIL=you@redhat.com
export JIRA_TOKEN=...

# Collect data: clones repos (first run ~60s), then parses git history (~30s)
make collect

# Print a text report
make report

# Or get JSON output
python3 cli.py report --json-output

# Launch Grafana dashboards (requires Docker)
make dashboard
# → http://localhost:3001 (admin/admin)

# Stop the dashboard
make dashboard-down
```

## Reports and AI agent integration

```bash
# Recurring failure pattern analysis (the main report)
make failure-patterns DAYS=30

# Weekly CI health digest
make digest

# Per-PR failure investigation (markdown)
make investigate PR=3346

# Export structured JSON context for AI agents
make export-context              # codebase-wide health
make export-context PR=3346      # per-PR context
make export-context OUTPUT=ctx.json  # write to file

# CI health report with charts (week / month / 3 months)
make ci-report                   # → data/ci-health-report.html
make ci-report OUTPUT=report.html  # custom path

# Bug bash deep analysis report with charts and recommendations
make bug-bash-report                    # → data/bug-bash-deep-analysis.html
make bug-bash-report OUTPUT=report.html # custom path

# JIRA collection analysis (requires JIRA_TOKEN + collections in config.yaml)
make jira-report COLLECTION=ai-bug-bash
make jira-report COLLECTION=ai-bug-bash JSON=1  # JSON output
```

## Architecture

See [docs/architecture.md](docs/architecture.md) for detailed data flow diagrams
and the full database schema.

```
cli.py                          # Click CLI — collect, report, export commands
collector/
  pr_collector.py               # GitHub PR data (from git log, zero API calls)
  branch_tracker.py             # Branch/release tracking
  repo_manager.py               # Git clone/update management
  ci_collector.py               # VictoriaMetrics + VictoriaLogs + GCS artifact ingestion
  jira_collector.py             # JIRA REST API — PR-referenced issues + label collections
  revert_detector.py            # Revert/cherry-pick detection
  code_analyzer.py              # Function-level code risk scoring
store/
  db.py                         # SQLite schema and data access
reports/
  failure_patterns.py           # Codebase-wide failure analysis + regression detection
  failure_investigation.py      # Per-PR investigation report
  weekly_digest.py              # Weekly summary with codebase-wide vs PR-specific split
  jira_report.py                # Per-collection JIRA analytics report
  json_export.py                # Structured JSON for AI agents
  assertion_parser.py           # Go test failure message parser
  links.py                      # URL builder for Prow, GitHub, Grafana, GCS
metrics/
  calculator.py                 # DORA metric computation
  ci_efficiency.py              # CI cycle/retry analysis
  git_ci_insights.py            # Component-level CI health correlation
  jira_analytics.py             # Standalone JIRA collection analytics
exporter/
  prometheus_exporter.py        # Prometheus metrics endpoint
dashboard/
  docker-compose.yml            # Local Grafana + Prometheus stack
  grafana/dashboards/           # Dashboard JSON files
  grafana/provisioning/         # Datasource and dashboard provisioning
```

## How regression onset detection works

For each broken test, the system:

1. **Builds a timeline** of pass/fail results across all builds, sorted chronologically
2. **Detects the pattern**:
   - If one PR accounts for >60% of failures → **PR under test** (that PR's code is wrong)
   - If failures span many PRs → **merged to main** (something merged broke it for everyone)
3. **Ranks causal candidates** by relevance to the failing test:
   - Component name overlap between test name and changed file paths
   - Code files (`internal/`, `pkg/`, `api/`) scored higher than docs/config
   - Non-code-only PRs (markdown, YAML) penalized

This produces output like: "kserve `Validate_component_enabled` started failing March 13
across 24 PRs — likely caused by PR#3257 which changed `kserve_controller.go`."

## Grafana dashboards

Dashboards are organized by audience:

| Folder | Dashboards |
|--------|-----------|
| **Engineering Leadership** | DORA metrics, deployment trends, release health |
| **CI & Test Health** | CI efficiency, build success rates, flake tracking, infra vs code |
| **Developer Tools** | PR investigation, component health, code risk hotspots |

## How each DORA metric is measured

### Deployment Frequency

> _How often does the team ship changes to production?_

| Signal | What it tells us | Source |
|--------|-----------------|--------|
| **Upstream releases** (`v*.*.*` tags) | How often the team cuts a formal release | `git for-each-ref refs/tags/` on bare clone |
| **PR merges to main** | How often changes enter the integration branch | `git log` on bare clone |
| **Downstream `rhoai-x.y` branches** | How often a release is staged for productization | `git for-each-ref` on downstream bare clone |

### Lead Time for Changes

> _How long from first commit to running in production?_

| Stage | Measurement | Source |
|-------|------------|--------|
| **PR cycle time** | Author date of first commit → committer date (merge time) | `git log --format=%aI,%cI` |
| **Merge → release tag** | Committer date of merge commit → creator date of earliest `v*` tag containing it | `git tag --contains` + `git for-each-ref` |

### Change Failure Rate

> _What percentage of changes cause a failure in production?_

| Failure signal | Why it indicates a failure | Source |
|---------------|--------------------------|--------|
| **Patch releases** | Hotfix release means the `.0` had a production bug | Tag patterns |
| **Reverts on main** | `Revert "..."` commit means a change was broken | `git log --grep` |
| **Cherry-picks to downstream** | Out-of-band fixes on frozen release branches | `git log --grep="cherry picked"` |

### Mean Time to Recovery (MTTR)

> _How quickly does the team recover from failures?_

| Recovery signal | Measurement | Source |
|----------------|------------|--------|
| **Patch release turnaround** | Time from `.0` to first `.1`/`.2` | Tag dates |

## Data sources

| Data | Source |
|------|--------|
| Release tags + dates | `git for-each-ref refs/tags/` |
| PR merge data | `git log --format=...` on main |
| Revert/cherry-pick detection | `git log --grep` |
| CI build results, step failures | VictoriaMetrics (from openshift-ci-observability) |
| CI failure messages | VictoriaLogs |
| JUnit test results | VictoriaLogs + GCS artifact fallback |
| Code risk scores | `gocyclo` / hotspot analysis |
| JIRA issue metadata | JIRA REST API (`JIRA_TOKEN` env var) |

## Documentation

| Document | Description |
|----------|-------------|
| [docs/architecture.md](docs/architecture.md) | Data flow, collector pipeline, DB schema, metrics computation |
| [docs/setup.md](docs/setup.md) | Prerequisites, installation, environment variables, CI stack setup |
| [docs/collectors.md](docs/collectors.md) | Each collector in detail, external APIs, and how to add new ones |
| [docs/metrics-reference.md](docs/metrics-reference.md) | Every metric available, where it comes from, how to access it |
| [AGENTS.md](AGENTS.md) | Agent-facing quick reference for AI coding assistants |

## Configuration

Edit `config.yaml` to adjust:
- Repository URLs and branch patterns
- Tag patterns for releases, EA builds, and patches
- Lookback period (default: 365 days)
- CI observability stack URLs (VictoriaMetrics, VictoriaLogs)
- Bot PR prefixes to filter (for cherry-pick detection)
- JIRA integration: base URL, project, custom field IDs, label-based collections

### JIRA setup

1. Set credentials as environment variables:
   - **Atlassian Cloud** (`*.atlassian.net`): set both `JIRA_EMAIL` and `JIRA_TOKEN` (API token)
   - **JIRA Server/DC**: set `JIRA_TOKEN` only (Personal Access Token)
2. In `config.yaml`, set `jira.enabled: true` and adjust `jira.base_url` if needed
3. Define collections under `jira.collections` with label patterns:

```yaml
jira:
  enabled: true
  base_url: https://redhat.atlassian.net
  project: RHOAIENG
  collections:
    - name: ai-bug-bash
      labels: ["ai-triage", "ai-fix", "ai-verified"]
      analyzer: bug-bash
      description: "AI-assisted bug bash"
    - name: ai-all
      label_prefix: "ai-"
      description: "All AI-labeled issues"
```

JIRA enrichment is optional — without `JIRA_TOKEN`, collection proceeds normally
and JIRA steps are skipped (same as `GITHUB_TOKEN` for releases).

## Data storage

All data is cached in `data/` (gitignored):
- `data/repos/*.git` — bare clones of both repos
- `data/eng-metrics.sqlite` — collected events and computed metrics

Run `make clean` to wipe and start fresh.
