# Setup Guide

## Prerequisites

- **Python 3.12+**
- **Git** (for bare clones of upstream/downstream repos)
- **Podman + podman-compose** (for CI Observability stack and Grafana dashboard)
- **Docker Compose** (for the local Grafana dashboard stack — or Podman with docker-compose compatibility)

### Optional tools

- **hotspots** or **gocyclo** — for code complexity analysis (`code_analyzer` collector)
- **matplotlib** — for standalone HTML report generation (`generate_bug_bash_report.py`)

## Installation

```bash
cd odh-eng-metrics
pip install -r requirements.txt
```

The `requirements.txt` includes:

| Package | Purpose |
|---------|---------|
| `click` | CLI framework |
| `httpx` | HTTP client (VictoriaMetrics, VictoriaLogs, JIRA, GitHub, GCS) |
| `gitpython` | Git repository operations |
| `pyyaml` | Config file parsing |
| `prometheus-client` | Prometheus exporter |

## Environment Variables

### Required for full functionality

| Variable | When needed | How to get it |
|----------|-------------|---------------|
| `JIRA_TOKEN` | JIRA enrichment | Atlassian Cloud: [API tokens](https://id.atlassian.com/manage-profile/security/api-tokens). JIRA Server/DC: Personal Access Token in user settings. |
| `JIRA_EMAIL` | Atlassian Cloud only | Your Atlassian account email. Required alongside `JIRA_TOKEN` for Basic auth on `*.atlassian.net`. |

### Optional

| Variable | Default | Purpose |
|----------|---------|---------|
| `GITHUB_TOKEN` | None | Higher rate limit for GitHub Releases API (used only for prerelease flags). Without it, the collector still works but may hit rate limits with many releases. |
| `CI_OBS_DIR` | `~/git/openshift-ci-observability` | Path to the `openshift-ci-observability` repo checkout. Used by the Makefile to auto-start the CI stack. |
| `PYTHON` | `python3` | Python interpreter path (Makefile variable). |

### Setting credentials

Add to your shell profile or a `.env` file you source before running:

```bash
export JIRA_EMAIL=you@redhat.com
export JIRA_TOKEN=your-atlassian-api-token
export GITHUB_TOKEN=ghp_your-github-token
```

Credentials are never stored in config files or the database. JIRA uses Basic
auth (base64 of `email:token`) for Cloud, or Bearer token for Server/DC.
HTTPS transport means credentials are encrypted in transit.

## CI Observability Stack

The CI collector requires VictoriaMetrics and VictoriaLogs with scraped CI
data. This is provided by the
[openshift-ci-observability](https://github.com/your-org/openshift-ci-observability)
project.

### First-time setup

```bash
# Clone the CI observability repo
git clone git@github.com:your-org/openshift-ci-observability.git ~/git/openshift-ci-observability

# Start the stack
cd ~/git/openshift-ci-observability
make up
```

This starts five containers:
- `ci-obs-victoriametrics` (port 8428) — time-series database
- `ci-obs-victorialogs` (port 9428) — log database
- `ci-obs-scraper-watch` — watches for new builds and scrapes them
- `ci-obs-scraper-backfill` — backfills historical builds
- `ci-obs-grafana` (port 3000) — Grafana dashboards

### Automatic management

The `make collect` target in odh-eng-metrics automatically checks that all five
containers are running via the `ensure-ci-obs` target. If any are missing, it
runs `make -C $CI_OBS_DIR restart` to bring up the full stack.

### GCS cache

The scraper maintains a local cache of GCS artifacts in a Podman volume
(`ci-obs-gcs-cache`). This cache can grow large (100GB+). Key details:

- Cache lives in a Podman volume, not the filesystem directly
- 404 responses are cached as `.miss` files with a 7-day TTL
- Run `make wipe-cache` in the CI observability repo to reclaim space
- Set `GCS_NO_CACHE=true` in the CI observability `.env` to disable caching

### Data ingestion timing

On first run, the scraper needs time to fetch and parse artifacts from GCS.
The CI collector waits up to `ci_observability.ingest_wait` seconds (default
180) before giving up. If you see "no CI data" on first run, wait a few
minutes and re-run `make collect`.

## Configuration (`config.yaml`)

The config file controls all collection behavior. Key sections:

### `upstream` / `downstream`

Repository definitions with clone URLs, branch names, and tag patterns:

```yaml
upstream:
  owner: opendatahub-io
  repo: opendatahub-operator
  clone_url: https://github.com/opendatahub-io/opendatahub-operator.git
  branches:
    main: main
    stable: stable
    downstream_staging: rhoai
  tags:
    release_pattern: 'v\d+\.\d+\.\d+$'
    ea_pattern: 'v\d+\.\d+\.\d+-ea\.\d+$'
    patch_pattern: 'v\d+\.\d+\.[1-9]\d*$'
```

### `jira`

JIRA integration settings and label-based collections:

```yaml
jira:
  enabled: true
  base_url: https://redhat.atlassian.net
  project: RHOAIENG
  issue_pattern: 'RHOAIENG-\d+'
  collections:
    - name: ai-bug-bash
      labels: ["ai-triaged", "ai-fixable", "ai-nonfixable"]
      analyzer: bug-bash
      description: "AI Bug Bash — March 2026"
```

Collections can match issues by:
- `labels` — explicit list of labels (issues with ANY of these labels)
- `label_prefix` — prefix match (e.g. `ai-` matches `ai-triaged`, `ai-fix`, etc.)
- `jql` — raw JQL query for full flexibility

### `ci_observability`

CI stack connection settings:

```yaml
ci_observability:
  enabled: true
  vm_url: http://localhost:8428
  vl_url: http://localhost:9428
  grafana_url: http://localhost:3000
  collect_steps: true
  collect_failure_messages: true
  ingest_wait: 180
```

### `collection`

General collection settings:

```yaml
collection:
  lookback_days: 365
  data_dir: data
  cache_db: data/eng-metrics.sqlite
```

## Local Development Overrides (`local.mk`)

Create a `local.mk` file in the project root to override Makefile variables
without modifying the tracked Makefile:

```makefile
PYTHON = python3.12
CI_OBS_DIR = /opt/ci-observability
```

## Grafana Dashboard Stack

The project includes a separate Grafana + Prometheus stack for visualizing
metrics. This is independent of the CI Observability Grafana.

```bash
make dashboard       # Start on ports 3001 (Grafana) and 9091 (Prometheus)
make dashboard-down  # Stop
make refresh         # Collect + restart exporter for fresh data
```

The dashboard stack connects to the CI Observability network so it can query
VictoriaMetrics alongside the local Prometheus exporter.

## Troubleshooting

### "CI Observability: stack not found"

The `CI_OBS_DIR` path doesn't point to a valid `openshift-ci-observability`
checkout. Clone the repo and set the variable, or accept that CI data will be
skipped.

### "VictoriaLogs had no test messages"

The scraper hasn't ingested JUnit XML yet. Check:
1. Is the scraper running? `podman ps | grep ci-obs-scraper`
2. Are there stale `.miss` cache files? Clear the GCS cache volume if needed
3. Wait for ingestion — check scraper logs with `podman logs ci-obs-scraper-watch`

### JIRA 410 Gone

Atlassian Cloud deprecated the v2 `/search` endpoint. The collector auto-detects
Cloud instances and uses the v3 `/search/jql` endpoint. If you see 410 errors,
ensure you're running the latest version of `jira_collector.py`.

### JIRA 429 Too Many Requests

The collector implements exponential backoff with retry on 429 responses, plus
a 100ms delay between all requests. If you still hit limits, increase the delay
or reduce collection frequency.

### "no code analysis tool available"

Install `hotspots` (preferred) or `gocyclo` for function-level risk scoring.
This is optional — all other collection proceeds normally without it.
