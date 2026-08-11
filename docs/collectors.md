# Collectors

Each collector is a Python module in `collector/` that fetches data from an
external source and persists it to SQLite via the `Store` class. This document
covers every collector in detail, plus a guide for adding new ones.

## Collector Inventory

### `repo_manager` — Git clone/fetch

**File:** `collector/repo_manager.py`

Manages bare clones of upstream and downstream repositories in `data/repos/`.
On first run, performs a full clone (~60s). On subsequent runs, does a fetch
(~5s). All other git-based collectors operate on these bare clones.

- **External source:** Git over HTTPS
- **Config:** `upstream.clone_url`, `downstream.clone_url`, `collection.data_dir`
- **Entry point:** `ensure_repos(cfg, data_dir) -> (Repo, Repo)`
- **DB tables:** None (provides repo objects to other collectors)

---

### `tag_collector` — Releases and downstream branches

**File:** `collector/tag_collector.py`

Collects upstream release tags (with dates and prerelease flags) and downstream
release branch metadata.

- **External sources:**
  - Git `for-each-ref refs/tags/` — tag names and dates
  - GitHub Releases API — prerelease flag (`GET /repos/{owner}/{repo}/releases`)
- **Config:** `upstream.tags.release_pattern`, `upstream.tags.ea_pattern`,
  `upstream.tags.patch_pattern`, `downstream.branches.release_pattern`
- **Env vars:** `GITHUB_TOKEN` (optional, for higher rate limits)
- **Entry points:**
  - `collect_upstream_releases(store, repo, cfg) -> int`
  - `collect_downstream_branches(store, repo, cfg) -> int`
- **DB tables:** `releases`, `downstream_branches`

---

### `pr_collector` — Merged PRs

**File:** `collector/pr_collector.py`

Extracts merged PR data from `git log` on the main branch. Zero GitHub API
calls — all data comes from commit messages and git metadata.

Parses from each merge commit:
- PR number (from `Merge pull request #N` or `(#N)` patterns)
- Title, author, dates (author date = created, committer date = merged)
- JIRA keys (regex: `RHOAIENG-\d+` from subject and body)
- Changed files and inferred component names
- AI-assisted markers

- **External source:** Git log only
- **Config:** `collection.lookback_days`
- **Entry point:** `collect_prs_from_git(store, repo_path, repo_name, branch, lookback_days) -> int`
- **DB tables:** `merged_prs`

---

### `revert_detector` — Revert commits

**File:** `collector/revert_detector.py`

Finds `Revert "..."` commits on upstream main. Links reverts back to the
original PR being reverted when possible.

- **External source:** Git log --grep
- **Config:** `collection.lookback_days`
- **Entry point:** `collect_reverts(store, repo, branch, lookback_days) -> int`
- **DB tables:** `reverts`

---

### `cherry_pick_detector` — Downstream backports

**File:** `collector/cherry_pick_detector.py`

Detects cherry-pick and backport commits on downstream release branches.
Filters out bot PRs using configurable prefixes.

- **External source:** Git log --grep="cherry picked"
- **Config:** `downstream.branches.release_pattern`, `downstream.bot_pr_prefixes`
- **Entry point:** `collect_cherry_picks(store, repo, cfg, lookback_days) -> int`
- **DB tables:** `cherry_picks`

---

### `branch_tracker` — PR propagation

**File:** `collector/branch_tracker.py`

Tracks when merge commits from PRs arrive at downstream branches (`stable`,
`rhoai`) and release tags. Answers "when did this PR reach production?"

- **External source:** Git (tag --contains, branch --contains)
- **Config:** `upstream.branches` (stable, downstream_staging)
- **Entry point:** `track_pr_propagation(store, repo_path, cfg) -> int`
- **DB tables:** `branch_arrivals`

---

### `ai_commit_detector` — AI-assisted commits

**File:** `collector/ai_commit_detector.py`

Scans all branches for commits with AI tool signatures:
- `Co-Authored-By: *cursor*` or `*copilot*` or `*claude*` etc.
- Cursor-specific trailers
- Other AI tool markers in commit messages

- **External source:** Git log --all
- **Config:** `collection.lookback_days`
- **Entry point:** `collect_ai_commits(store, repo_path, repo_name, lookback_days) -> int`
- **DB tables:** `ai_assisted_commits`

---

### `ci_collector` — CI build data

**File:** `collector/ci_collector.py`

The most complex collector. Queries the CI Observability stack for build-level
and test-level data:

1. **Builds** — VictoriaMetrics PromQL queries for `ci_build_duration_seconds`,
   build results, resource metrics (CPU, memory)
2. **Steps** — VictoriaMetrics for step-level durations and outcomes
3. **Failure messages** — VictoriaLogs LogsQL for step and test failure text
4. **Test results** — VictoriaLogs for JUnit leaf test outcomes, with GCS
   artifact fallback when VL lacks data

The collector also classifies steps as infrastructure vs code using pattern
matching (`INFRA_STEP_PATTERNS`).

- **External sources:**
  - VictoriaMetrics (PromQL): `ci_build_duration_seconds`, `ci_pipeline_step_duration_seconds`, resource metrics
  - VictoriaLogs (LogsQL): `source:"junit_step"`, `source:"junit_test"` log queries
  - GCS (`test-platform-results` bucket): JUnit XML fallback via HTTPS
- **Config:** `ci_observability.*` (vm_url, vl_url, collect_steps, collect_failure_messages, ingest_wait)
- **Entry point:** `collect_ci_builds(store, cfg, lookback_days) -> int`
- **DB tables:** `ci_builds`, `ci_build_steps`, `ci_build_failure_messages`, `ci_test_results`

#### Data flow

```
VictoriaMetrics                        VictoriaLogs
    │                                      │
    ├─ ci_build_duration_seconds ──────────┤─ junit_step messages
    ├─ ci_pipeline_step_duration_seconds   ├─ junit_test messages
    ├─ build result labels                 │
    └─ resource metrics (cpu, mem)         │
                                           │
    All → SQLite tables                    │
                                           │
                           If VL has no test messages:
                                           │
                                    GCS JUnit XML
                                    (test-platform-results bucket)
                                           │
                                    Parse XML → test results
```

---

### `jira_collector` — JIRA issue metadata

**File:** `collector/jira_collector.py`

Two collection modes:

1. **PR-referenced issues** — fetches metadata for JIRA keys found in PR
   commit messages
2. **Collection issues** — fetches all issues matching a collection's labels/JQL

Supports both JIRA Cloud (v3 API with Atlassian Document Format) and JIRA
Server/DC (v2 API). Auto-detects based on whether the base URL contains
`atlassian.net`.

Features:
- Rate limiting with exponential backoff on 429 responses
- 100ms delay between all requests
- Incremental collection — only re-fetches issues older than 4 hours
- Comment fetching for collection issues
- ADF (Atlassian Document Format) → plain text conversion

- **External source:** JIRA REST API
  - Cloud: `POST /rest/api/3/search/jql` (token-based pagination)
  - Server: `POST /rest/api/2/search` (offset-based pagination)
  - Comments: `GET /rest/api/{version}/issue/{key}/comment`
- **Config:** `jira.*` (base_url, project, issue_pattern, collections)
- **Env vars:** `JIRA_TOKEN`, `JIRA_EMAIL` (Cloud only)
- **Entry points:**
  - `collect_pr_issues(store, cfg) -> int`
  - `collect_collection(store, cfg, collection_cfg) -> int`
- **DB tables:** `jira_issues`, `jira_collection_issues`

---

### `code_analyzer` — Code risk scoring

**File:** `collector/code_analyzer.py`

Computes function-level risk scores combining cyclomatic complexity with git
churn (changes in last 30 days). Requires a non-bare checkout of the repository.

Prefers `hotspots` CLI, falls back to `gocyclo`. Both are optional — if neither
is available, this collector is silently skipped.

- **External source:** Local CLI tools (hotspots, gocyclo) + Git log
- **Config:** None (uses hardcoded path to non-bare repo)
- **Entry point:** `analyze_code_risk(store, repo_path, repo_name) -> int`
- **DB tables:** `code_risk_scores`

---

### `github_client` — GitHub Releases API

**File:** `collector/github_client.py`

Thin wrapper around the GitHub Releases API. Used exclusively by `tag_collector`
to get the `prerelease` flag for release tags.

- **External source:** `GET /repos/{owner}/{repo}/releases`
- **Env vars:** `GITHUB_TOKEN`

---

## Adding a New Collector

### Step 1: Create the collector module

Create `collector/<name>.py` with a collection function:

```python
"""Collect <thing> from <source>."""

from __future__ import annotations

import logging
from store.db import Store

log = logging.getLogger(__name__)


def collect_things(store: Store, cfg: dict, lookback_days: int = 365) -> int:
    """Collect <things> and store them.
    
    Returns the number of items collected.
    """
    # 1. Read config
    source_url = cfg.get("my_source", {}).get("url")
    if not source_url:
        log.info("my_source not configured, skipping")
        return 0

    # 2. Query external source
    items = _fetch_from_source(source_url, lookback_days)

    # 3. Persist to Store
    for item in items:
        store.upsert_thing(item)

    log.info("Collected %d things", len(items))
    return len(items)
```

Key conventions:
- Function signature: `collect_*(store, cfg, ...) -> int`
- Return count of items collected (0 is valid — means "nothing to do")
- Log at INFO for progress, DEBUG for detail
- Handle missing config gracefully (return 0, don't crash)
- Use `httpx.Client` for HTTP requests (consistent with other collectors)

### Step 2: Add database tables

In `store/db.py`, add the table to `SCHEMA`:

```python
SCHEMA = """
...existing tables...

CREATE TABLE IF NOT EXISTS my_things (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    value       REAL,
    collected_at TEXT NOT NULL
);
"""
```

Add upsert and get methods to the `Store` class:

```python
def upsert_thing(self, thing: dict) -> None:
    self.conn.execute(
        """INSERT OR REPLACE INTO my_things
           (id, name, value, collected_at)
           VALUES (?, ?, ?, ?)""",
        (thing["id"], thing["name"], thing.get("value"),
         thing["collected_at"]),
    )
    self.conn.commit()

def get_things(self) -> list[dict]:
    return [dict(r) for r in self.conn.execute(
        "SELECT * FROM my_things ORDER BY name"
    ).fetchall()]
```

If adding columns to an existing table, add a migration entry in `_migrate()`:

```python
def _migrate(self) -> None:
    migrations = [
        ...existing migrations...
        ("my_things", "new_column", "TEXT"),
    ]
```

### Step 3: Wire into the collection pipeline

In `cli.py`, import the collector and add it to the `collect()` command:

```python
from collector import my_collector

@cli.command()
def collect() -> None:
    ...existing collectors...

    click.echo("Collecting things from my source...")
    n = my_collector.collect_things(store, cfg, lookback_days=lookback)
    if n > 0:
        click.echo(f"  {n} things stored")
    else:
        click.echo("  no things found (or source not configured)")
```

Choose the pipeline position carefully — if your collector depends on data from
other collectors (e.g., it needs PR data), place it after those collectors.

### Step 4: Add configuration (if needed)

Add a section to `config.yaml`:

```yaml
my_source:
  enabled: true
  url: https://api.example.com
  api_key_env: MY_SOURCE_TOKEN  # read from env var at runtime
```

### Step 5: Add metrics (optional)

If the collected data supports computed analytics, create `metrics/<name>.py`:

```python
def compute_thing_metrics(things: list[dict]) -> dict:
    """Compute analytics from collected things."""
    return {
        "total": len(things),
        "by_category": _group_by(things, "category"),
        ...
    }
```

Wire it into `metrics/calculator.py:compute_all()` and add it to the report
templates.

### Step 6: Add to reports (optional)

Add sections to existing report generators in `reports/` or create a new CLI
command if the data warrants a standalone report.

### Testing

Add tests in `tests/` covering the core collection logic. Use the existing
pattern of mocking external APIs and verifying Store contents.

### Checklist

- [ ] `collector/<name>.py` with `collect_*()` → int
- [ ] DB table(s) in `store/db.py` SCHEMA + Store methods
- [ ] Wired into `cli.py:collect()` pipeline
- [ ] Config section in `config.yaml` (if needed)
- [ ] Environment variable documented (if needed)
- [ ] Handles missing config / unavailable source gracefully
- [ ] Logging at appropriate levels
- [ ] Idempotent (safe to run multiple times)
