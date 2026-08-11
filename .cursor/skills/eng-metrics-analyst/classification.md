# Metric Definitions and Classification Rules

## DORA Metrics

### Deployment Frequency
Measures how often the team ships to production.

**Data source**: `releases` table (stable tags only, `prerelease = 0`).

| Classification | Criteria |
|----------------|----------|
| Elite | Multiple deploys per day |
| High | Between once per day and once per week |
| Medium | Between once per week and once per month |
| Low | More than once per month |

**Also computed at PR level**: average gap between `merged_at` timestamps in `merged_prs` where `base_branch = 'main'`.

### Lead Time for Changes
Time from first commit to production.

**Stages** (each computed as percentiles from `merged_prs` + `branch_arrivals`):

| Stage | Calculation |
|-------|-------------|
| PR cycle time | `merged_at - first_commit_at` |
| PR review time | `merged_at - created_at` |
| To stable branch | `branch_arrivals.arrived_at - merged_prs.merged_at` where branch = `stable` |
| To rhoai branch | Same, where branch matches `rhoai-*` |
| To tagged release | `releases.published - merged_prs.merged_at` |

### Change Failure Rate
Fraction of changes that result in failures requiring remediation.

**Failure events** (summed from multiple signals):
- Patch releases: `releases WHERE is_patch = 1`
- Reverts on main: `reverts` table
- Cherry-pick hotfixes: `cherry_picks` table (non-bot, human-authored)

**Rate**: `total_failure_events / total_changes * 100`

### Mean Time to Recovery (MTTR)
How quickly failures are remediated.

**Primary signal**: time between a stable release and its subsequent patch release.

```sql
-- Patch turnaround: gap between stable release and its patch
SELECT r2.tag, r2.published, r1.published,
       (julianday(r2.published) - julianday(r1.published)) * 24 AS hours
FROM releases r1
JOIN releases r2 ON r2.is_patch = 1
  AND r2.tag LIKE substr(r1.tag, 1, instr(r1.tag, '.')) || '%'
WHERE r1.is_patch = 0 AND r1.prerelease = 0
```

## CI Efficiency Metrics

All computed from `ci_builds` + `ci_build_steps` + `ci_test_results`.

| Metric | Definition |
|--------|------------|
| First-pass success rate | % of PRs whose first CI build passed |
| Retest tax | Average builds per PR (`COUNT(*) / COUNT(DISTINCT pr_number)`) |
| Cycle failure rate | % of builds with `result != 'success'` |
| Wasted CI hours | `SUM(duration_seconds) WHERE result != 'success'` / 3600 |
| Infra vs code failures | Join `ci_build_steps WHERE level = 'Error'` — `is_infra = 1` for infra |

### Grouping builds into test cycles
A "test cycle" is all builds for a single PR. Use `GROUP BY pr_number`.

```sql
-- First-pass success rate
SELECT
  COUNT(CASE WHEN first_result = 'success' THEN 1 END) * 100.0 / COUNT(*) AS first_pass_pct
FROM (
  SELECT pr_number, result AS first_result
  FROM ci_builds
  WHERE (pr_number, started_at) IN (
    SELECT pr_number, MIN(started_at) FROM ci_builds GROUP BY pr_number
  )
)
```

## PR Flow Metrics

| Metric | SQL sketch |
|--------|------------|
| Throughput (monthly) | `SELECT strftime('%Y-%m', merged_at), COUNT(*) FROM merged_prs GROUP BY 1` |
| Cycle time distribution | Bucket `(julianday(merged_at) - julianday(first_commit_at)) * 24` into ranges |
| PR size | `additions + deletions` |
| AI-assisted ratio | `SUM(is_ai_assisted) * 100.0 / COUNT(*)` from `merged_prs` |

## Code Risk

Computed from `code_risk_scores`. Risk bands:

| Band | Score Range | Meaning |
|------|-------------|---------|
| Critical | >= 8.0 | High complexity + high churn — top refactoring priority |
| High | 5.0 - 7.9 | Needs attention |
| Medium | 3.0 - 4.9 | Moderate |
| Low | < 3.0 | Healthy |

**Component risk summary**:
```sql
SELECT component,
       COUNT(*) AS functions,
       SUM(CASE WHEN risk_band = 'Critical' THEN 1 ELSE 0 END) AS critical,
       ROUND(AVG(risk_score), 2) AS avg_risk
FROM code_risk_scores
WHERE component IS NOT NULL
GROUP BY component
ORDER BY avg_risk DESC
```

## Agent Readiness

From `agentready_assessments`. Certification levels:

| Level | Score | Meaning |
|-------|-------|---------|
| Gold | >= 80 | Fully ready for AI bug automation |
| Silver | 60-79 | Mostly ready, some gaps |
| Bronze | 40-59 | Partially ready |
| Not Ready | < 40 | Significant gaps |

Detailed findings are in `findings_json` (JSON blob with per-attribute scores).

## AI Adoption

From `ai_assisted_commits`. Tools detected by commit message patterns:
- `copilot` — GitHub Copilot (Co-authored-by trailer)
- `cursor` — Cursor AI
- `aider` — Aider CLI
- Others as detected

**Monthly trend**:
```sql
SELECT strftime('%Y-%m', date) AS month,
       COUNT(DISTINCT sha) AS ai_commits,
       tool
FROM ai_assisted_commits
GROUP BY month, tool
ORDER BY month
```

## JIRA Analytics

### Status categories
JIRA maps granular statuses to three categories:
- **To Do**: `New`, `Backlog`, `Refinement`, `To Do`
- **In Progress**: `In Progress`, `Code Review`, `QE Review`
- **Done**: `Closed`, `Done`, `Resolved`, `Verified`

### Resolution time
```sql
SELECT key, summary,
       ROUND((julianday(resolved) - julianday(created)) * 24, 1) AS hours_to_resolve
FROM jira_issues
WHERE resolved IS NOT NULL
ORDER BY hours_to_resolve DESC
```

### Component distribution (unnesting JSON)
```sql
SELECT je.value AS component, COUNT(*) AS issues
FROM jira_issues ji, json_each(ji.components) je
GROUP BY je.value
ORDER BY issues DESC
```

## Cross-Reference Patterns

### JIRA issues linked to PRs
PRs reference JIRA keys in their title/body. The `jira_keys` column on `merged_prs` stores the extracted keys.

```sql
SELECT ji.key, ji.summary, mp.repo, mp.number, mp.title
FROM jira_issues ji, json_each(mp.jira_keys) je
JOIN merged_prs mp ON je.value = ji.key
WHERE ji.status_category != 'Done'
```

### PR -> CI build chain
```sql
SELECT mp.number, mp.title, cb.build_id, cb.result, cb.duration_seconds
FROM merged_prs mp
JOIN ci_builds cb ON cb.pr_number = mp.number
ORDER BY mp.number, cb.started_at
```
