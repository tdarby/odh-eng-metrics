---
name: eng-metrics-analyst
description: >-
  Analyze engineering metrics from the odh-eng-metrics SQLite database covering
  DORA metrics, CI efficiency, PR flow, code risk, JIRA analytics, agent
  readiness, and AI adoption. Use when asked about engineering health, release
  cadence, CI pass rates, PR throughput, code hotspots, JIRA issue trends, or
  any quantitative question about the opendatahub-operator development process.
---

# Engineering Metrics Analyst

Analyze the opendatahub-operator engineering data stored in a local SQLite database. The database is populated by collectors that pull from Git history, GitHub APIs, JIRA, and CI Observability (VictoriaMetrics). You query it directly with SQL.

## Principles

- **Compute on demand**: derive metrics from raw data via SQL rather than relying on pre-computed values in `metrics_cache`. The cache may be stale.
- **Evidence-based**: every claim backed by a query result. Show the SQL.
- **Cross-reference**: the power of this dataset is joining across sources -- JIRA issues to PRs to CI builds to code risk. Use it.
- **Interpret, don't just count**: raw numbers need context. Compare to baselines, compute rates, identify trends.
- **Causation over correlation**: a test failing *during* a PR's CI build does not mean that PR caused the failure. Always trace back to what actually changed on the base branch. See the root cause attribution workflow below.

## Data Sources

The database at `data/eng-metrics.sqlite` contains data from:

| Source | Tables | What |
|--------|--------|------|
| Git (upstream) | `merged_prs`, `reverts`, `releases` | PR metadata, revert detection, release tags |
| Git (downstream) | `cherry_picks`, `downstream_branches`, `branch_arrivals` | Hotfix tracking, propagation timing |
| Git + GitHub API | `component_manifest_pins`, `manifest_sha_deltas` | Upstream SHA pins per component, changelogs between bumps |
| GitHub API | `ai_assisted_commits` | AI tool adoption in commits |
| JIRA REST API | `jira_issues`, `jira_collection_issues` | Issue lifecycle, labels, components |
| CI Observability | `ci_builds`, `ci_build_steps`, `ci_test_results`, `ci_build_failure_messages` | Build results, step timing, test outcomes |
| Static analysis | `code_risk_scores` | Function-level complexity and churn |
| AgentReady tool | `agentready_assessments` | Per-repo AI automation readiness |

## Query Tool

All queries go through `.cursor/skills/eng-metrics-analyst/eng-query`. Run from the `odh-eng-metrics` directory.

```bash
python .cursor/skills/eng-metrics-analyst/eng-query status          # Health check
python .cursor/skills/eng-metrics-analyst/eng-query schema          # All table DDL
python .cursor/skills/eng-metrics-analyst/eng-query collections     # JIRA collections
python .cursor/skills/eng-metrics-analyst/eng-query sql "SELECT …"  # Ad-hoc query (JSON lines)
```

The `sql` command is your primary tool. It enforces read-only mode. Output is one JSON object per row.

**Always run `status` first** to confirm the database exists and has data.

## References

- Full table schemas and column semantics: [schema.md](schema.md)
- Metric definitions, DORA classification, and example SQL: [classification.md](classification.md)

Read these before writing queries. They document JSON column formats, primary keys, and cross-reference join patterns.

## Report Generation Skills

For structured CI health reports rendered as interactive HTML canvases, follow one of these sub-workflows:

- **Weekly CI Digest** — [ci-digest.md](ci-digest.md): week-over-week KPIs, new breakages, resolutions, component health, and a narrative "state of CI" summary. Use when asked for a weekly digest, CI summary, or quick CI status check.
- **CI Deep Analysis** — [ci-deep-analysis.md](ci-deep-analysis.md): multi-period KPI comparison (7d/30d/90d), trend charts, error clustering, manifest regression detection, code risk correlation, and prioritized recommendations. Use when asked for a deep CI analysis, trend report, or comprehensive CI health assessment.

Both skills produce dual output: an HTML canvas for interactive follow-up in Cursor, and a shareable HTML file saved to `reports/` with a timestamped filename. Read the relevant skill file and follow its query workflow and output structure.

## Investigation Workflows

### "How healthy is engineering?"

1. **Release cadence**: count stable releases, compute average gap
2. **PR throughput**: monthly merge counts, trend direction
3. **CI efficiency**: first-pass success rate, retest tax
4. **Code risk**: critical/high hotspot count, worst components
5. **Open JIRA aging**: p50/p90 age of open issues

### "Is CI getting better or worse?"

1. Monthly breakdown: `GROUP BY strftime('%Y-%m', started_at)`
2. Compare first-pass success rate across months
3. Identify top failing steps: `GROUP BY step_name WHERE level = 'Error'`
4. Check if flaky tests dominate: same test failing intermittently across PRs
5. Infrastructure vs code: `is_infra` flag on `ci_build_steps`

### "Which components need attention?"

1. Code risk: highest `avg_risk_score` by component
2. JIRA: most open bugs by component (unnest `components` JSON)
3. CI: which components' tests fail most (join `ci_test_results` to test name patterns)
4. Agent readiness: lowest `overall_score` in `agentready_assessments`
5. Cross-reference: components that are high-risk AND have many open bugs

### "How is AI adoption trending?"

1. Monthly AI-assisted commits from `ai_assisted_commits`
2. `is_ai_assisted` flag on `merged_prs` for PR-level view
3. Tool breakdown: which AI tools are being used
4. Quality signal: do AI-assisted PRs have different CI pass rates?

### "Which PRs broke this test?" — Root Cause Attribution

Attributing CI test failures to PRs requires care. Two common mistakes to avoid:

1. **Correlation ≠ causation**: A test failing *during* PR #X's CI build does NOT mean PR #X caused the failure. CI builds run the PR's changes against the current `main` branch. The breakage may have been introduced by a *different* PR already merged to main. The `ci_test_results` → `ci_builds` join gives you which PR was *under test*, not which PR *introduced the bug*.

2. **JIRA keys describe intent, not impact**: The `jira_keys` field on `merged_prs` describes what the PR was *trying to accomplish*. Never attribute a JIRA to a test failure just because the test happened to fail during that PR's CI run.

**Correct methodology — work backwards from the breakage window:**

1. **Establish the breakage boundary**: Find the first failure timestamp for the test. Check whether the test was passing before that date — `ci_test_results` now stores both `passed` and `failed` leaf tests. Query for `status = 'passed'` rows with the same `test_name` before the failure date. If the test has no prior rows at all, it was likely newly introduced.

2. **Identify candidate PRs**: If `ci_builds.base_sha` is populated, use it to find exactly which PRs were on main when the build ran (`JOIN merged_prs mp ON mp.merge_sha <= cb.base_sha`). Otherwise, query `merged_prs` for PRs merged to `main` in the window *before* the first failure. Filter by `changed_files` or `changed_components` that overlap with the test file or the code under test. For component-level test failures, also check for manifest SHA bumps (`is_manifest_update = 1`) — these pull in new upstream resources that can break component tests without any operator code change.

3. **Check for upstream manifest regressions**: If a manifest SHA bump PR landed in the breakage window, query `component_manifest_pins` to find what SHA changed, then check `manifest_sha_deltas` for the upstream commit changelog between the old and new SHA. This reveals whether the upstream component introduced a breaking change — look for commits that modify CRDs, change default configs, or alter API behavior. When the delta exists, include the upstream commits in your analysis before blaming operator code.

4. **Distinguish new-test vs. regression**: If the test name doesn't appear in *any* `ci_test_results` rows before the failure date, it was likely *introduced already failing* (or flaky from the start). The PR that added it is the root cause — look for PRs that modified the test file.

5. **Attribute JIRA only from the causal PR**: Once you've identified the PR that actually introduced the breakage, use *that* PR's `jira_keys` (if any). Many infrastructure/test PRs have no JIRA — say so explicitly rather than borrowing a JIRA from an unrelated PR. For PRs not yet merged, check `ci_pr_metadata` for title/author/jira_keys (fetched from GitHub API).

6. **Flag flaky vs. deterministic**: If the test fails on many unrelated PRs with low frequency (1-2 per PR, spread across many PRs), it's likely flaky/timing-sensitive rather than a deterministic breakage.

**Example queries:**
```sql
-- Was this test passing before it broke?
SELECT status, COUNT(*) AS cnt, MIN(cb.started_at) AS earliest
FROM ci_test_results tr
JOIN ci_builds cb ON tr.build_id = cb.build_id
WHERE tr.test_name = 'TestOdhOperator/services/group_1/monitoring/Test_Prometheus_rules_lifecycle'
GROUP BY status;

-- Find PRs that touched monitoring code in the breakage window
SELECT number, title, author, jira_keys, merged_at, changed_files
FROM merged_prs
WHERE repo = 'opendatahub-io/opendatahub-operator'
  AND base_branch = 'main'
  AND merged_at BETWEEN '2026-03-16' AND '2026-03-19'
  AND (changed_files LIKE '%monitoring%' OR changed_files LIKE '%e2e/monitoring%')
ORDER BY merged_at;

-- Find manifest SHA bumps in the breakage window
SELECT number, title, author, jira_keys, merged_at
FROM merged_prs
WHERE repo = 'opendatahub-io/opendatahub-operator'
  AND base_branch = 'main'
  AND merged_at BETWEEN '2026-03-24' AND '2026-03-27'
  AND is_manifest_update = 1
ORDER BY merged_at;

-- Use base_sha to find exactly what was on main when a build ran
SELECT cb.build_id, cb.base_sha, mp.number, mp.title
FROM ci_builds cb
JOIN merged_prs mp ON mp.merge_sha = cb.base_sha
WHERE cb.pr_number = 3300;

-- Get title/author for open PRs referenced in CI (COALESCE across both tables)
SELECT cb.pr_number,
       COALESCE(mp.title, cpm.title) AS title,
       COALESCE(mp.author, cpm.author) AS author,
       COALESCE(mp.jira_keys, cpm.jira_keys) AS jira_keys
FROM ci_builds cb
LEFT JOIN merged_prs mp ON mp.number = cb.pr_number
LEFT JOIN ci_pr_metadata cpm ON cpm.number = cb.pr_number
WHERE mp.title IS NULL AND cpm.title IS NOT NULL;

-- Check if a component had a manifest SHA bump in the breakage window
SELECT cmp.component, cmp.pinned_sha, cmp.captured_at, cmp.pr_number,
       msd.old_sha, msd.commit_count, msd.commits_json
FROM component_manifest_pins cmp
LEFT JOIN manifest_sha_deltas msd
    ON msd.component = cmp.component AND msd.new_sha = cmp.pinned_sha
WHERE cmp.component = 'kserve'
  AND cmp.captured_at BETWEEN '2026-03-24' AND '2026-03-28'
ORDER BY cmp.captured_at DESC;
```

### Ad-hoc questions

For any question not covered above:

1. Identify which tables contain the relevant data (check schema.md)
2. Write a SQL query — the model is very good at this
3. Run via `eng-query sql "..."`
4. Interpret results in context

## Key SQL Patterns

### Unnesting JSON arrays
Many columns store JSON arrays as TEXT. Use SQLite's `json_each()`:
```sql
SELECT ji.key, je.value AS label
FROM jira_issues ji, json_each(ji.labels) je
WHERE je.value LIKE 'ai-%'
```

### Time bucketing
```sql
SELECT strftime('%Y-%m', merged_at) AS month, COUNT(*) AS prs
FROM merged_prs
WHERE base_branch = 'main'
GROUP BY month ORDER BY month
```

### First-per-group (e.g. first CI build per PR)
```sql
SELECT * FROM ci_builds
WHERE rowid IN (
  SELECT rowid FROM ci_builds b2
  WHERE b2.pr_number = ci_builds.pr_number
  ORDER BY b2.started_at LIMIT 1
)
```

### Cross-source joins
```sql
-- JIRA issues → linked PRs → CI builds
SELECT ji.key, ji.summary, mp.number, cb.result
FROM jira_issues ji
JOIN merged_prs mp ON mp.jira_keys LIKE '%' || ji.key || '%'
JOIN ci_builds cb ON cb.pr_number = mp.number
```

## Tips

- **JSON columns**: `labels`, `components`, `fix_versions`, `jira_keys`, `changed_files`, `changed_components` are all JSON TEXT. Use `json_each()` or `LIKE '%value%'` for quick filtering.
- **Date math**: SQLite uses `julianday()` for date arithmetic. Hours = `(julianday(a) - julianday(b)) * 24`.
- **Large result sets**: add `LIMIT 20` during exploration, remove for final analysis.
- **NULL handling**: `resolved IS NULL` means the issue is still open. Many columns are nullable.
- **Collection scoping**: to analyze a specific JIRA collection, join through `jira_collection_issues`.
