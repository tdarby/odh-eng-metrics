# Database Schema Reference

SQLite database at `data/eng-metrics.sqlite`. All dates are ISO-8601 strings. JSON arrays are stored as TEXT (use `json_each()` to unnest).

## Core Tables

### releases
Upstream git tags (stable, EA, patch).

| Column | Type | Notes |
|--------|------|-------|
| tag | TEXT PK | e.g. `v3.5.0`, `v3.5.1-ea.1` |
| published | TEXT | ISO date |
| prerelease | INT | 1 = prerelease (EA) |
| is_patch | INT | 1 = patch release (3rd digit > 0) |
| is_ea | INT | 1 = early-access |

### merged_prs
PRs merged to upstream branches. One row per PR.

| Column | Type | Notes |
|--------|------|-------|
| repo | TEXT | `owner/repo` |
| number | INT | PR number |
| title | TEXT | |
| author | TEXT | GitHub username |
| created_at | TEXT | PR opened |
| merged_at | TEXT | Merge timestamp |
| first_commit_at | TEXT | Earliest commit in PR (for cycle time) |
| base_branch | TEXT | Target branch |
| additions | INT | Lines added |
| deletions | INT | Lines removed |
| jira_keys | TEXT | JSON array of JIRA keys, e.g. `["RHOAIENG-123"]` |
| merge_sha | TEXT | Merge commit SHA |
| is_ai_assisted | INT | 1 = AI commit markers detected |
| changed_files | TEXT | JSON array of file paths |
| changed_components | TEXT | JSON array of component names |
| is_manifest_update | INT | 1 = PR changed manifest SHAs or `get_all_manifests.sh` |

**PK**: (repo, number)

### reverts
Revert commits detected on main.

| Column | Type | Notes |
|--------|------|-------|
| repo | TEXT | |
| sha | TEXT PK | Revert commit SHA |
| date | TEXT | |
| reverted_sha | TEXT | Original commit that was reverted |
| message | TEXT | Commit message |
| reverted_pr | INT | PR number of the reverted change (nullable) |

### cherry_picks
Cherry-pick PRs on downstream release branches.

| Column | Type | Notes |
|--------|------|-------|
| repo | TEXT | |
| pr_number | INT | |
| target_branch | TEXT | e.g. `rhoai-2.16` |
| title | TEXT | |
| author | TEXT | |
| merged_at | TEXT | |

**PK**: (repo, pr_number)

### downstream_branches
Downstream release branches (e.g. `rhoai-2.15`, `rhoai-2.16-ea.1`).

| Column | Type | Notes |
|--------|------|-------|
| name | TEXT PK | Branch name |
| first_commit_date | TEXT | When first commit landed |
| is_ea | INT | 1 = EA branch |

### branch_arrivals
Tracks when an upstream PR's changes arrive on a downstream branch.

| Column | Type | Notes |
|--------|------|-------|
| pr_repo | TEXT | |
| pr_number | INT | |
| branch | TEXT | Target branch |
| arrived_at | TEXT | Timestamp |

**PK**: (pr_repo, pr_number, branch)

## CI Tables

### ci_builds
One row per CI build (Prow job run).

| Column | Type | Notes |
|--------|------|-------|
| build_id | TEXT PK | Prow build ID |
| pr_number | INT | |
| job_name | TEXT | e.g. `pull-ci-opendatahub-io-opendatahub-operator-main-unit` |
| duration_seconds | REAL | Total wall clock |
| result | TEXT | `success`, `failure`, `aborted` |
| started_at | TEXT | |
| peak_cpu_cores | REAL | Max CPU usage |
| peak_memory_bytes | REAL | Max memory |
| total_step_seconds | REAL | Sum of all step durations |
| base_sha | TEXT | SHA of main branch at presubmit time (from GCS started.json) |
| pull_sha | TEXT | SHA of the PR head at presubmit time |

### ci_build_steps
Per-step breakdown within a build.

| Column | Type | Notes |
|--------|------|-------|
| build_id | TEXT | FK to ci_builds |
| step_name | TEXT | e.g. `e2e-odh`, `unit` |
| duration_seconds | REAL | |
| level | TEXT | Severity: `Error`, `Warning`, `Info` |
| is_infra | INT | 1 = infrastructure step (not test code) |

**PK**: (build_id, step_name)

### ci_build_failure_messages
Failure messages extracted from CI logs.

| Column | Type | Notes |
|--------|------|-------|
| build_id | TEXT | FK to ci_builds |
| message | TEXT | Truncated to 500 chars |
| source | TEXT | Where extracted from |
| count | INT | Occurrences in build |

**PK**: (build_id, message)

### ci_test_results
JUnit test results from CI. Both passed and failed leaf tests are collected.

| Column | Type | Notes |
|--------|------|-------|
| build_id | TEXT | FK to ci_builds |
| test_name | TEXT | Go test path |
| suite | TEXT | Test suite name |
| test_variant | TEXT | e2e variant |
| status | TEXT | `passed`, `failed`, `skipped` |
| duration_seconds | REAL | |
| is_leaf | INT | 1 = leaf test (no children) |
| failure_message | TEXT | Enriched from VictoriaLogs or GCS JUnit XML |

**PK**: (build_id, test_name, test_variant)

### ci_pr_metadata
Metadata for PRs referenced in `ci_builds` but not in `merged_prs` (open or closed-without-merge). Fetched from GitHub API.

| Column | Type | Notes |
|--------|------|-------|
| repo | TEXT | `owner/repo` |
| number | INT | PR number |
| title | TEXT | |
| author | TEXT | GitHub username |
| state | TEXT | `open`, `closed` |
| jira_keys | TEXT | JSON array of JIRA keys extracted from title + body |
| changed_files | TEXT | JSON array of file paths |
| changed_components | TEXT | JSON array of component names |
| fetched_at | TEXT | When we last fetched from GitHub |

**PK**: (repo, number)

**Querying with merged_prs**: To get title/author/jira_keys for any PR number, COALESCE across both tables:
```sql
SELECT cb.pr_number,
       COALESCE(mp.title, cpm.title) AS title,
       COALESCE(mp.author, cpm.author) AS author,
       COALESCE(mp.jira_keys, cpm.jira_keys) AS jira_keys
FROM ci_builds cb
LEFT JOIN merged_prs mp ON mp.number = cb.pr_number
LEFT JOIN ci_pr_metadata cpm ON cpm.number = cb.pr_number
```

## Component Manifest Tracking Tables

### component_manifest_pins
Tracks which upstream SHA each component is pinned to at each manifest-update PR (and at current HEAD). Parsed from `get_all_manifests.sh` in the operator repo.

| Column | Type | Notes |
|--------|------|-------|
| component | TEXT | Manifest key, e.g. `kserve`, `dashboard`, `ray` |
| repo_url | TEXT | Upstream repo URL, e.g. `https://github.com/opendatahub-io/kserve` |
| branch | TEXT | Upstream branch (nullable if bare SHA) |
| pinned_sha | TEXT | Commit SHA the component is pinned to |
| source_path | TEXT | Path within the upstream repo, e.g. `config` |
| captured_at | TEXT | When this pin was recorded (merge timestamp of the PR, or current time for HEAD) |
| pr_number | INT | Operator PR that set this pin (null for HEAD snapshot) |

**PK**: (component, captured_at)

### manifest_sha_deltas
Upstream commit changelog between consecutive SHA bumps for a component. Fetched from GitHub compare API.

| Column | Type | Notes |
|--------|------|-------|
| component | TEXT | Same as `component_manifest_pins.component` |
| old_sha | TEXT | Previous pinned SHA |
| new_sha | TEXT | New pinned SHA |
| repo_url | TEXT | Upstream repo URL |
| commit_count | INT | Total commits between old and new SHA |
| commits_json | TEXT | JSON array of `{"sha", "message", "author", "date"}` (capped at 50) |
| pr_number | INT | Operator PR that bumped the SHA |
| fetched_at | TEXT | When the compare was fetched |

**PK**: (component, old_sha, new_sha)

**Correlating manifest bumps with test failures:**
```sql
-- "KServe test broke on Mar 26. Was there a manifest SHA bump?"
SELECT cmp.component, cmp.pinned_sha, cmp.captured_at,
       msd.old_sha, msd.commit_count, msd.commits_json
FROM component_manifest_pins cmp
LEFT JOIN manifest_sha_deltas msd
    ON msd.component = cmp.component AND msd.new_sha = cmp.pinned_sha
WHERE cmp.component = 'kserve'
ORDER BY cmp.captured_at DESC
LIMIT 5;
```

## JIRA Tables

### jira_issues
Full issue metadata fetched from JIRA REST API.

| Column | Type | Notes |
|--------|------|-------|
| key | TEXT PK | e.g. `RHOAIENG-12345` |
| summary | TEXT | Issue title |
| issue_type | TEXT | `Bug`, `Story`, `Task`, etc. |
| priority | TEXT | `Blocker`, `Critical`, `Major`, `Normal`, `Minor` |
| status | TEXT | e.g. `New`, `In Progress`, `Closed` |
| status_category | TEXT | `To Do`, `In Progress`, `Done` |
| assignee | TEXT | Display name |
| components | TEXT | JSON array: `["Dashboard", "KServe"]` |
| labels | TEXT | JSON array: `["ai-triaged", "sprint-42"]` |
| fix_versions | TEXT | JSON array |
| story_points | REAL | |
| created | TEXT | |
| resolved | TEXT | Resolution date (null if open) |
| epic_key | TEXT | Parent epic |
| description | TEXT | Full description text |
| comments | TEXT | JSON array of `{"author", "body", "created"}` |
| fetched_at | TEXT | When we last pulled from JIRA |

**Querying JSON arrays**: Use `json_each()` to unnest. Example:
```sql
SELECT ji.key, je.value AS label
FROM jira_issues ji, json_each(ji.labels) je
WHERE je.value LIKE 'ai-%'
```

### jira_collection_issues
Many-to-many: which issues belong to which named collections.

| Column | Type | Notes |
|--------|------|-------|
| collection_name | TEXT | e.g. `ai-bug-bash` |
| issue_key | TEXT | FK to jira_issues.key |

**PK**: (collection_name, issue_key)

### metrics_cache
Pre-computed metric values (key-value store).

| Column | Type | Notes |
|--------|------|-------|
| metric | TEXT | Metric name |
| window | TEXT | Time window or qualifier |
| value | TEXT | JSON-encoded value |
| computed_at | TEXT | |

**PK**: (metric, window)

## Analysis Tables

### ai_assisted_commits
Commits with AI tool markers (Co-authored-by, tool tags).

| Column | Type | Notes |
|--------|------|-------|
| repo | TEXT | |
| sha | TEXT | Commit SHA |
| date | TEXT | |
| message | TEXT | First 200 chars |
| tool | TEXT | `copilot`, `cursor`, `aider`, etc. |

**PK**: (repo, sha, tool)

### code_risk_scores
Per-function code risk from static analysis (cyclomatic complexity + churn).

| Column | Type | Notes |
|--------|------|-------|
| repo | TEXT | |
| file | TEXT | File path |
| function | TEXT | Function name |
| component | TEXT | Mapped component |
| complexity | REAL | Cyclomatic complexity |
| churn_30d | INT | Commits touching this function in 30 days |
| risk_score | REAL | Combined risk (0-10 scale) |
| risk_band | TEXT | `Low`, `Medium`, `High`, `Critical` |
| analyzed_at | TEXT | |

**PK**: (repo, file, function)

### agentready_assessments
AI bug automation readiness scores per repository.

| Column | Type | Notes |
|--------|------|-------|
| repo_url | TEXT | GitHub URL |
| project | TEXT | JIRA project key |
| overall_score | REAL | 0-100 |
| certification_level | TEXT | e.g. `Gold`, `Silver`, `Bronze`, `Not Ready` |
| attributes_assessed | INT | |
| attributes_total | INT | |
| findings_json | TEXT | JSON blob with detailed findings |
| assessed_at | TEXT | |

**PK**: (repo_url, project)
