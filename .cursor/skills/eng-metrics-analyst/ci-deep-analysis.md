# CI Deep Analysis

Generate an interactive HTML canvas with multi-period CI health analysis, trend charts, failure pattern clustering, and dynamic root cause analysis.

## Prerequisites

1. Read [schema.md](schema.md) for table structures and JSON column formats.
2. Run `python .cursor/skills/eng-metrics-analyst/eng-query status` to confirm data exists.
3. All queries use `eng-query sql "..."` from the `odh-eng-metrics` directory.

## Query Workflow

### Step 1: Multi-period KPIs

Run the same KPI query for three periods. Substitute `{CUTOFF}` with the appropriate date.

```sql
SELECT COUNT(*) AS total_builds,
       COUNT(DISTINCT pr_number) AS total_prs,
       SUM(CASE WHEN result = 'success' THEN 1 ELSE 0 END) AS passed,
       SUM(CASE WHEN result != 'success' THEN 1 ELSE 0 END) AS failed,
       ROUND(SUM(CASE WHEN result != 'success' THEN duration_seconds ELSE 0 END) / 3600.0, 1) AS wasted_hours
FROM ci_builds
WHERE started_at >= '{CUTOFF}'
```

Periods:
- **Week**: `date('now', '-7 days')`
- **Month**: `date('now', '-30 days')`
- **3 Months**: `date('now', '-90 days')`

For first-pass rate per period:
```sql
SELECT COUNT(*) AS total,
       SUM(CASE WHEN result = 'success' THEN 1 ELSE 0 END) AS first_pass_ok
FROM ci_builds
WHERE (pr_number, started_at) IN (
  SELECT pr_number, MIN(started_at) FROM ci_builds
  WHERE started_at >= '{CUTOFF}' GROUP BY pr_number
)
```

Retest tax per period:
```sql
SELECT ROUND(CAST(COUNT(*) AS REAL) / COUNT(DISTINCT pr_number), 2) AS retest_tax
FROM ci_builds WHERE started_at >= '{CUTOFF}'
```

### Step 2: Weekly trend data (for line/bar charts)

```sql
SELECT strftime('%Y-W%W', started_at) AS week,
       date(started_at, 'weekday 1', '-6 days') AS week_start,
       COUNT(*) AS total,
       SUM(CASE WHEN result = 'success' THEN 1 ELSE 0 END) AS passed,
       SUM(CASE WHEN result != 'success' THEN 1 ELSE 0 END) AS failed,
       ROUND(SUM(CASE WHEN result != 'success' THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 1) AS fail_pct
FROM ci_builds
WHERE started_at >= date('now', '-90 days')
GROUP BY week
ORDER BY week
```

### Step 3: Infrastructure vs code failures (per period)

```sql
SELECT
  SUM(CASE WHEN bs.is_infra = 1 THEN 1 ELSE 0 END) AS infra_errors,
  SUM(CASE WHEN bs.is_infra = 0 THEN 1 ELSE 0 END) AS code_errors
FROM ci_build_steps bs
JOIN ci_builds cb ON bs.build_id = cb.build_id
WHERE cb.started_at >= '{CUTOFF}' AND cb.result != 'success' AND bs.level = 'Error'
```

### Step 4: Test health distribution

```sql
SELECT test_name,
       SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS fails,
       COUNT(*) AS runs,
       ROUND(SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 1) AS fail_pct
FROM ci_test_results tr
JOIN ci_builds cb ON tr.build_id = cb.build_id
WHERE cb.started_at >= date('now', '-30 days') AND tr.is_leaf = 1
GROUP BY test_name
HAVING runs >= 3
ORDER BY fail_pct DESC
```

Classify results:
- **Broken**: fail_pct > 80
- **Flaky**: fail_pct between 20 and 80
- **Healthy**: fail_pct < 20

### Step 5: Top failing steps (3 months)

```sql
SELECT step_name,
       COUNT(*) AS errors,
       MAX(is_infra) AS is_infra,
       COUNT(DISTINCT bs.build_id) AS affected_builds
FROM ci_build_steps bs
JOIN ci_builds cb ON bs.build_id = cb.build_id
WHERE cb.started_at >= date('now', '-90 days') AND bs.level = 'Error'
GROUP BY step_name
ORDER BY errors DESC
LIMIT 15
```

### Step 6: Component health (month)

```sql
SELECT je.value AS component,
       COUNT(DISTINCT cb.build_id) AS builds,
       SUM(CASE WHEN cb.result != 'success' THEN 1 ELSE 0 END) AS failures,
       ROUND(SUM(CASE WHEN cb.result != 'success' THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 1) AS fail_pct,
       COUNT(DISTINCT cb.pr_number) AS prs
FROM merged_prs mp
JOIN ci_builds cb ON cb.pr_number = mp.number, json_each(mp.changed_components) je
WHERE mp.merged_at >= date('now', '-30 days') AND mp.base_branch = 'main'
GROUP BY je.value
HAVING builds >= 3
ORDER BY fail_pct DESC
```

### Step 7: Cycle duration percentiles (per period)

```sql
SELECT ROUND(AVG(duration_seconds) / 60.0, 1) AS mean_min,
       ROUND(duration_seconds / 60.0, 1) AS individual_min
FROM ci_builds
WHERE started_at >= '{CUTOFF}' AND duration_seconds IS NOT NULL
```

For true percentiles, fetch all durations and compute in the canvas JS, or use:
```sql
SELECT ROUND(AVG(duration_seconds) / 60.0, 1) AS mean_min
FROM ci_builds WHERE started_at >= '{CUTOFF}' AND duration_seconds IS NOT NULL
```

### Step 8: Manifest regression detection

```sql
-- Recent manifest-update PRs (last 30 days) using the is_manifest_update flag
SELECT number, title, merged_at
FROM merged_prs
WHERE base_branch = 'main'
  AND merged_at >= date('now', '-30 days')
  AND is_manifest_update = 1
ORDER BY merged_at DESC
```

For each manifest PR, get the upstream SHA deltas to see what component code changed:
```sql
-- Upstream changelog for manifest bumps in the last 30 days
SELECT cmp.component, cmp.pr_number, cmp.captured_at,
       msd.old_sha, msd.new_sha, msd.commit_count, msd.commits_json
FROM component_manifest_pins cmp
LEFT JOIN manifest_sha_deltas msd
    ON msd.component = cmp.component AND msd.new_sha = cmp.pinned_sha
WHERE cmp.pr_number IS NOT NULL
  AND cmp.captured_at >= date('now', '-30 days')
ORDER BY cmp.captured_at DESC
```

Then compare step failure rates before vs after each manifest PR:
```sql
-- Before the manifest PR merged
SELECT step_name,
       SUM(CASE WHEN level = 'Error' THEN 1 ELSE 0 END) AS errors,
       COUNT(*) AS total
FROM ci_build_steps bs
JOIN ci_builds cb ON bs.build_id = cb.build_id
WHERE cb.started_at < '{MANIFEST_MERGE_DATE}'
  AND cb.started_at >= date('{MANIFEST_MERGE_DATE}', '-14 days')
GROUP BY step_name

-- After the manifest PR merged
SELECT step_name,
       SUM(CASE WHEN level = 'Error' THEN 1 ELSE 0 END) AS errors,
       COUNT(*) AS total
FROM ci_build_steps bs
JOIN ci_builds cb ON bs.build_id = cb.build_id
WHERE cb.started_at >= '{MANIFEST_MERGE_DATE}'
  AND cb.started_at < date('{MANIFEST_MERGE_DATE}', '+14 days')
GROUP BY step_name
```

A regression is: after_error_rate - before_error_rate > 15pp AND after_error_rate > 25%.

When a regression is detected and a manifest delta exists for the same component, include the upstream commit messages in the report — this helps the team decide whether to revert the bump or fix the upstream issue.

### Step 9: Code risk correlation

```sql
SELECT component,
       COUNT(*) AS functions,
       SUM(CASE WHEN risk_band = 'Critical' THEN 1 ELSE 0 END) AS critical,
       SUM(CASE WHEN risk_band = 'High' THEN 1 ELSE 0 END) AS high,
       ROUND(AVG(risk_score), 2) AS avg_risk
FROM code_risk_scores
WHERE component IS NOT NULL
GROUP BY component
ORDER BY avg_risk DESC
```

Cross-reference with component health from Step 6: components with high failure rate AND high code risk are priority targets.

### Step 10: Error message clustering

```sql
SELECT SUBSTR(message, 1, 120) AS msg_prefix,
       COUNT(*) AS occurrences,
       COUNT(DISTINCT fm.build_id) AS builds_affected
FROM ci_build_failure_messages fm
JOIN ci_builds cb ON fm.build_id = cb.build_id
WHERE cb.started_at >= date('now', '-30 days')
GROUP BY msg_prefix
HAVING occurrences >= 3
ORDER BY occurrences DESC
LIMIT 15
```

## Dynamic Analysis

After collecting all data, perform these deeper analyses and embed findings in the canvas.

### Trend interpretation

Compare week vs month vs 3-month KPIs:
- Is failure rate trending up or down? By how much?
- Is retest tax improving? What does the weekly trend line show?
- Are wasted CI hours increasing?

Write a narrative: "CI health is [improving/degrading/stable] over the past 3 months. The failure rate has [risen/fallen] from X% to Y%. The primary driver is [infra/code/flaky tests]."

### Regression onset for broken tests

For each broken test (fail_pct > 80):
1. Find the earliest failure in the last 30 days
2. Find PRs merged just before that date
3. Score candidate PRs by component overlap with the test name
4. Check `component_manifest_pins` for manifest SHA bumps in the breakage window for the same component — if found, query `manifest_sha_deltas` for the upstream changelog and include it in the suspected cause
5. Report: "Test X has been broken since {date}, likely caused by PR #{N}" — or "likely caused by upstream changes in {component} (manifest bump PR #{N}, {commit_count} upstream commits)"

### Code risk correlation

For each component with fail_pct > 30%:
- Look up its code risk scores
- If avg_risk > 5.0 or critical functions > 3, flag as "high-risk component with CI problems"
- Recommend: refactoring priority

### Temporal patterns

```sql
SELECT CASE CAST(strftime('%w', started_at) AS INTEGER)
         WHEN 0 THEN 'Sun' WHEN 1 THEN 'Mon' WHEN 2 THEN 'Tue'
         WHEN 3 THEN 'Wed' WHEN 4 THEN 'Thu' WHEN 5 THEN 'Fri' WHEN 6 THEN 'Sat'
       END AS day_of_week,
       COUNT(*) AS builds,
       ROUND(SUM(CASE WHEN result != 'success' THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 1) AS fail_pct
FROM ci_builds
WHERE started_at >= date('now', '-30 days')
GROUP BY day_of_week
ORDER BY CAST(strftime('%w', started_at) AS INTEGER)
```

If any day has notably higher failure rate, note it.

### Prioritized recommendations

Rank all findings by estimated CI hours saved if fixed:
1. Broken tests (blocking every PR) - highest impact
2. Top flaky tests (causing retests) - multiply fails * avg_duration
3. Infrastructure patterns - if consistent, report upstream
4. Manifest regressions - revert the bump or fix the upstream issue (cite specific upstream commits from `manifest_sha_deltas`)
5. High-risk components - longer-term refactoring

## Output

Produce **both** outputs:

1. **Canvas** — for immediate interactive viewing and follow-up questions in Cursor.
2. **HTML file** — for sharing via Slack, email, or archiving.

### HTML file output

Write the report as a self-contained HTML file to:

```
reports/ci-deep-analysis-YYYY-MM-DD.html
```

(relative to the `odh-eng-metrics` directory, where `YYYY-MM-DD` is today's date.)

Create the `reports/` directory if it doesn't exist. The HTML must be fully self-contained — all CSS inline, Chart.js loaded from CDN, no external files except Google Fonts. Anyone can open it in a browser with no setup.

After writing the file, tell the user the path so they can share it.

### Canvas output

Also render the same HTML as a canvas titled "CI Deep Analysis - opendatahub-operator" for interactive follow-up in Cursor.

### Required sections (both outputs)

1. **Multi-Period KPI Dashboard** - 3-column grid comparing week/month/3-month: failure rate, first-pass %, retest tax, wasted hours, build count. Each with trend arrows (green up = improvement, red = regression).

2. **Trend Narrative** (dynamic) - Agent-written 3-5 sentence analysis of CI trajectory.

3. **Weekly Failure Rate** - Line chart (Chart.js) with failure rate % over the last 3 months, weekly data points. Add a dashed average line.

4. **Weekly Build Volume** - Stacked bar chart: passed (green) vs failed (red) per week.

5. **Infrastructure vs Code** - Three donut charts side-by-side (week/month/3-month) showing infra vs code split.

6. **Test Health** - Three stat boxes (broken/flaky/healthy counts) + table of broken tests with: name, fail rate, regression onset, suspected cause.

7. **Component Health** - Horizontal bar chart of failure rate by component. Overlay code risk score as a secondary indicator.

8. **Manifest Regressions** (if detected) - Table: component, manifest PR, before rate, after rate, delta, upstream commit count, notable upstream commits (from `manifest_sha_deltas.commits_json`).

9. **Top Failing Steps** - Horizontal bar chart, purple for infra, blue for code.

10. **Error Clusters** - Table: error message pattern, occurrences, builds affected.

11. **Temporal Patterns** - Bar chart of failure rate by day of week.

12. **Recommendations** (dynamic) - Numbered list, prioritized by CI hours saved. Each with severity badge and specific action.

### Design

- Dark theme: `background: #0f0f23`, cards `#16213e`, borders `#333355`
- Colors: success `#66bb6a`, failure `#ef5350`, warning `#ffa726`, infra `#ab47bc`, code `#42a5f5`, primary `#4fc3f7`, accent `#26c6da`
- Font: Import `JetBrains Mono` for data, `DM Sans` for prose from Google Fonts
- Chart.js via CDN: `https://cdn.jsdelivr.net/npm/chart.js`
- Dynamic analysis sections: `border-left: 4px solid #26c6da; background: #1a2744`
- KPI cards with trend arrows: green `#66bb6a` for improvement, red `#ef5350` for regression
- Use CSS grid for responsive layout; each chart section gets its own card
