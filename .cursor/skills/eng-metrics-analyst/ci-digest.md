# CI Weekly Digest

Generate an interactive HTML canvas summarizing this week's CI health with week-over-week comparison and dynamic analysis.

## Prerequisites

1. Read [schema.md](schema.md) for table structures and JSON column formats.
2. Run `python .cursor/skills/eng-metrics-analyst/eng-query status` to confirm data exists.
3. All queries use `eng-query sql "..."` from the `odh-eng-metrics` directory.

## Query Workflow

Run these queries in order. Store results for the canvas and for dynamic analysis.

### Step 1: Date boundaries

Compute ISO date strings for this week (Mon-Sun) and last week. Use today's date. Example for a Wednesday:

```sql
SELECT date('now', 'weekday 1', '-7 days') AS this_monday,
       date('now') AS today,
       date('now', 'weekday 1', '-14 days') AS last_monday
```

Use these dates in all subsequent `WHERE` clauses. Call them `THIS_MON`, `LAST_MON`, and `TODAY`.

### Step 2: Headline KPIs

```sql
-- This week builds
SELECT COUNT(*) AS total_builds,
       SUM(CASE WHEN result = 'success' THEN 1 ELSE 0 END) AS passed,
       SUM(CASE WHEN result != 'success' THEN 1 ELSE 0 END) AS failed,
       COUNT(DISTINCT pr_number) AS prs_with_ci,
       ROUND(SUM(CASE WHEN result != 'success' THEN duration_seconds ELSE 0 END) / 3600.0, 1) AS wasted_hours
FROM ci_builds
WHERE started_at >= '{THIS_MON}' AND started_at < '{TODAY}'
```

Run the same query for last week (`>= LAST_MON AND < THIS_MON`).

```sql
-- PRs merged this week
SELECT COUNT(*) AS prs_merged
FROM merged_prs
WHERE base_branch = 'main' AND merged_at >= '{THIS_MON}'
```

```sql
-- Reverts this week
SELECT COUNT(*) AS reverts FROM reverts WHERE date >= '{THIS_MON}'
```

```sql
-- First-pass success rate (this week)
SELECT COUNT(*) AS total,
       SUM(CASE WHEN result = 'success' THEN 1 ELSE 0 END) AS first_pass_ok
FROM ci_builds
WHERE (pr_number, started_at) IN (
  SELECT pr_number, MIN(started_at) FROM ci_builds
  WHERE started_at >= '{THIS_MON}' GROUP BY pr_number
)
```

### Step 3: Test health

```sql
-- Per-test failure rates this week
SELECT tr.test_name,
       SUM(CASE WHEN tr.status = 'failed' THEN 1 ELSE 0 END) AS fails,
       COUNT(*) AS runs,
       ROUND(SUM(CASE WHEN tr.status = 'failed' THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 1) AS fail_pct
FROM ci_test_results tr
JOIN ci_builds cb ON tr.build_id = cb.build_id
WHERE cb.started_at >= '{THIS_MON}' AND tr.is_leaf = 1
GROUP BY tr.test_name
HAVING runs >= 2
ORDER BY fail_pct DESC
```

Run the same query for last week. Compare to detect:
- **New breakages**: fail_pct > 50 this week, < 20 last week
- **Resolved**: fail_pct > 50 last week, < 10 this week
- **Ongoing broken**: fail_pct > 80 both weeks
- **Worsening flakes**: 20-80% this week, increase > 15pp from last week

### Step 4: Top failing steps

```sql
SELECT step_name, COUNT(*) AS errors, MAX(is_infra) AS is_infra
FROM ci_build_steps
WHERE build_id IN (SELECT build_id FROM ci_builds WHERE started_at >= '{THIS_MON}' AND result != 'success')
  AND level = 'Error'
GROUP BY step_name
ORDER BY errors DESC
LIMIT 10
```

### Step 5: Component health

```sql
-- Failure rate by component (from PR changed_components)
SELECT je.value AS component,
       COUNT(DISTINCT cb.build_id) AS builds,
       SUM(CASE WHEN cb.result != 'success' THEN 1 ELSE 0 END) AS failures,
       ROUND(SUM(CASE WHEN cb.result != 'success' THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 1) AS fail_pct
FROM merged_prs mp
JOIN ci_builds cb ON cb.pr_number = mp.number, json_each(mp.changed_components) je
WHERE mp.merged_at >= '{THIS_MON}' AND mp.base_branch = 'main'
GROUP BY je.value
HAVING builds >= 3
ORDER BY fail_pct DESC
```

### Step 6: JIRA context (optional, run if jira_issues has data)

```sql
-- Open bugs linked to PRs that failed CI this week
SELECT ji.key, ji.summary, ji.priority, ji.status, ji.components
FROM jira_issues ji
WHERE ji.key IN (
  SELECT DISTINCT je.value FROM merged_prs mp, json_each(mp.jira_keys) je
  JOIN ci_builds cb ON cb.pr_number = mp.number
  WHERE cb.started_at >= '{THIS_MON}' AND cb.result != 'success'
)
AND ji.status_category != 'Done'
ORDER BY ji.priority
```

### Step 7: Manifest SHA bumps this week

```sql
-- Manifest-update PRs merged this week
SELECT mp.number, mp.title, mp.merged_at
FROM merged_prs mp
WHERE mp.base_branch = 'main'
  AND mp.merged_at >= '{THIS_MON}'
  AND mp.is_manifest_update = 1
ORDER BY mp.merged_at
```

If any manifest PRs were found, get the upstream changelogs:
```sql
-- Upstream SHA deltas for this week's manifest bumps
SELECT cmp.component, cmp.pr_number, cmp.pinned_sha,
       msd.old_sha, msd.new_sha, msd.commit_count, msd.commits_json
FROM component_manifest_pins cmp
LEFT JOIN manifest_sha_deltas msd
    ON msd.component = cmp.component AND msd.new_sha = cmp.pinned_sha
WHERE cmp.pr_number IN (
    SELECT number FROM merged_prs
    WHERE base_branch = 'main' AND merged_at >= '{THIS_MON}' AND is_manifest_update = 1
)
ORDER BY cmp.component
```

### Step 8: AI adoption signal

```sql
SELECT is_ai_assisted,
       COUNT(*) AS prs,
       SUM(CASE WHEN cb_fail > 0 THEN 1 ELSE 0 END) AS prs_with_failures
FROM (
  SELECT mp.number, mp.is_ai_assisted,
         SUM(CASE WHEN cb.result != 'success' THEN 1 ELSE 0 END) AS cb_fail
  FROM merged_prs mp
  LEFT JOIN ci_builds cb ON cb.pr_number = mp.number AND cb.started_at >= '{THIS_MON}'
  WHERE mp.merged_at >= '{THIS_MON}' AND mp.base_branch = 'main'
  GROUP BY mp.number, mp.is_ai_assisted
)
GROUP BY is_ai_assisted
```

## Dynamic Analysis

After collecting all data, perform these analyses. Write your findings directly into the canvas HTML as narrative paragraphs in `<div class="analysis">` blocks.

### Root cause hypotheses for new breakages

For each test that newly broke this week:
1. Query `ci_test_results` to find the first failure timestamp
2. Query `merged_prs` for PRs merged just before that timestamp
3. Check if the test name contains a component name matching the PR's `changed_components`
4. Check `component_manifest_pins` for manifest SHA bumps in the breakage window for the same component — if found, query `manifest_sha_deltas` for the upstream changelog
5. Write a hypothesis: "Test X likely broke due to PR #N (merged YYYY-MM-DD, touched component Y)" — or if a manifest bump is the likely cause: "Test X likely broke due to upstream changes in {component} pulled in by manifest bump PR #{N} ({commit_count} upstream commits)"

### Cross-reference patterns

- If multiple broken tests share a component, note the shared root cause
- If infra failures dominate, note the infrastructure pattern
- If AI-assisted PRs have notably different pass rates, highlight it

### Narrative summary

Write a 2-3 sentence "State of CI" paragraph that answers: "Is CI healthy this week? What's the biggest risk? What action should the team take?"

## Output

Produce **both** outputs:

1. **Canvas** — for immediate interactive viewing and follow-up questions in Cursor.
2. **HTML file** — for sharing via Slack, email, or archiving.

### HTML file output

Write the report as a self-contained HTML file to:

```
reports/ci-digest-YYYY-MM-DD.html
```

(relative to the `odh-eng-metrics` directory, where `YYYY-MM-DD` is today's date.)

Create the `reports/` directory if it doesn't exist. The HTML must be fully self-contained — all CSS inline, Chart.js loaded from CDN, no external files except Google Fonts. Anyone can open it in a browser with no setup.

After writing the file, tell the user the path so they can share it.

### Canvas output

Also render the same HTML as a canvas titled "CI Weekly Digest - {date range}" for interactive follow-up in Cursor.

### Required sections (both outputs)

1. **KPI Cards** - Grid of stat boxes: PRs merged, failure rate (with delta arrow), first-pass %, retest tax, wasted hours, reverts. Each with week-over-week comparison.

2. **State of CI** (dynamic) - Agent-written narrative paragraph.

3. **New Breakages** - Table: test name, fail rate, runs, suspected cause (from dynamic analysis).

4. **Resolved This Week** - Table: test name, was fail rate, now passing.

5. **Ongoing Broken** - Table: test name, this week rate, last week rate.

6. **Component Health** - Horizontal bar chart (Chart.js) of failure rate by component.

7. **Manifest Bumps** (if any this week) - Table: component, manifest PR, upstream commit count, notable upstream commit messages (from `manifest_sha_deltas.commits_json`). Flag components where a bump coincides with a new test breakage.

8. **Infrastructure vs Code** - Donut chart showing the split.

9. **Top Failing Steps** - Horizontal bar chart with infra steps in purple, code steps in blue.

10. **JIRA Context** (if data exists) - Table of open bugs linked to failing PRs.

11. **AI Adoption** (if data exists) - Comparison of AI vs human PR pass rates.

### Design

- Dark theme: `background: #0f0f23`, cards `#16213e`, borders `#333355`
- Colors: success `#66bb6a`, failure `#ef5350`, warning `#ffa726`, infra `#ab47bc`, code `#42a5f5`, primary `#4fc3f7`
- Font: Import `JetBrains Mono` for data, `DM Sans` for prose from Google Fonts
- Chart.js via CDN: `https://cdn.jsdelivr.net/npm/chart.js`
- Dynamic analysis sections get a left-border accent: `border-left: 4px solid #26c6da`
