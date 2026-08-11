"""Weekly CI health digest.

Generates a concise summary of the past week's CI activity, highlighting
regressions, persistent failures, infrastructure trends, and component risk
for quick team review or AI agent processing.

Enriched with individual test results, regression onset detection, and
causal PR identification.
"""

from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from metrics import ci_efficiency
from reports.assertion_parser import format_for_report, format_for_table, parse_failure_message
from reports.failure_patterns import (
    _detect_regression_onset,
    _is_wrapper_message,
    _test_name_to_file,
)
from reports.links import LinkBuilder
from store.db import Store

log = logging.getLogger(__name__)


def _parse_json_field(value: str | None) -> list:
    if not value:
        return []
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []


def _fmt_pct(value: float | None) -> str:
    return f"{value * 100:.0f}%" if value is not None else "N/A"


def generate(store: Store, weeks_back: int = 1,
             links: LinkBuilder | None = None) -> str:
    """Generate a weekly CI health digest covering the last N weeks."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(weeks=weeks_back)
    cutoff_str = cutoff.strftime("%Y-%m-%d")
    prev_cutoff = cutoff - timedelta(weeks=weeks_back)
    prev_cutoff_str = prev_cutoff.strftime("%Y-%m-%d")

    all_prs = store.get_merged_prs(base_branch="main")
    week_prs = [p for p in all_prs if (p.get("merged_at") or "") >= cutoff_str]
    prev_prs = [p for p in all_prs
                if prev_cutoff_str <= (p.get("merged_at") or "") < cutoff_str]

    all_builds = store.get_ci_builds()
    week_pr_nums = {p["number"] for p in week_prs}
    prev_pr_nums = {p["number"] for p in prev_prs}
    week_builds = [b for b in all_builds if b["pr_number"] in week_pr_nums]
    prev_builds = [b for b in all_builds if b["pr_number"] in prev_pr_nums]

    week_summary = ci_efficiency.compute_summary(week_builds)
    prev_summary = ci_efficiency.compute_summary(prev_builds)

    reverts = store.get_reverts()
    week_reverts = [r for r in reverts if (r.get("date") or "") >= cutoff_str]

    all_steps = store.get_all_build_steps()
    all_fail_msgs = store.get_all_build_failure_messages()
    week_build_ids = {b["build_id"] for b in week_builds}
    failed_build_ids = {b["build_id"] for b in week_builds if b["result"] == "failure"}

    pr_components: dict[int, list[str]] = {}
    for p in week_prs:
        comps = _parse_json_field(p.get("changed_components"))
        if comps:
            pr_components[p["number"]] = comps

    # ---- Test results for this week and previous week ----
    all_test_results = store.get_all_test_results()
    build_map: dict[str, dict] = {b["build_id"]: b for b in all_builds}

    week_test_failures: list[dict] = []
    prev_test_failures: list[dict] = []
    for t in all_test_results:
        if not t.get("is_leaf") or t["status"] != "failed":
            continue
        binfo = build_map.get(t["build_id"])
        if not binfo:
            continue
        if binfo["pr_number"] in week_pr_nums:
            week_test_failures.append(t)
        elif binfo["pr_number"] in prev_pr_nums:
            prev_test_failures.append(t)

    # Test aggregation for this week
    week_test_builds: dict[str, set[str]] = defaultdict(set)
    week_test_msg: dict[str, str] = {}
    week_test_bid: dict[str, str] = {}
    for t in week_test_failures:
        tname = t["test_name"]
        week_test_builds[tname].add(t["build_id"])
        if t.get("failure_message") and tname not in week_test_msg:
            week_test_msg[tname] = t["failure_message"]
        if tname not in week_test_bid:
            week_test_bid[tname] = t["build_id"]

    # How many builds ran each test this week?
    week_test_total: dict[str, set[str]] = defaultdict(set)
    for t in all_test_results:
        if t.get("is_leaf") and build_map.get(t["build_id"], {}).get("pr_number") in week_pr_nums:
            week_test_total[t["test_name"]].add(t["build_id"])

    # Same for prev week
    prev_test_builds: dict[str, set[str]] = defaultdict(set)
    for t in prev_test_failures:
        prev_test_builds[t["test_name"]].add(t["build_id"])

    prev_test_total: dict[str, set[str]] = defaultdict(set)
    for t in all_test_results:
        if t.get("is_leaf") and build_map.get(t["build_id"], {}).get("pr_number") in prev_pr_nums:
            prev_test_total[t["test_name"]].add(t["build_id"])

    lines: list[str] = []
    _w = lines.append

    period_label = f"Week of {cutoff.strftime('%b %d')} – {now.strftime('%b %d, %Y')}"
    _w("# Weekly CI Health Digest")
    _w(f"**{period_label}**")
    _w("")

    # ===================================================================
    # Headlines
    # ===================================================================
    _w("## Headlines")
    _w("")
    _w(f"- **PRs merged:** {len(week_prs)}"
       f" (prev week: {len(prev_prs)}, "
       f"{'↑' if len(week_prs) > len(prev_prs) else '↓' if len(week_prs) < len(prev_prs) else '→'}"
       f")")

    fr = week_summary.get("cycle_failure_rate")
    prev_fr = prev_summary.get("cycle_failure_rate")
    fr_delta = ""
    if fr is not None and prev_fr is not None:
        diff = (fr - prev_fr) * 100
        if abs(diff) >= 1:
            fr_delta = f" ({'↑' if diff > 0 else '↓'}{abs(diff):.0f}pp vs prev week)"
    _w(f"- **Cycle failure rate:** {_fmt_pct(fr)}{fr_delta}")

    fps = week_summary.get("first_pass_success_rate")
    _w(f"- **First-pass success:** {_fmt_pct(fps)}")

    rt = week_summary.get("retest_tax")
    _w(f"- **Retest tax:** {rt:.2f} cycles/PR" if rt else "- **Retest tax:** N/A")

    _w(f"- **Reverts this week:** {len(week_reverts)}")

    # Headline test counts
    n_broken = sum(
        1 for tname, bids in week_test_builds.items()
        if len(bids) >= 3
        and week_test_total.get(tname)
        and len(bids) / len(week_test_total[tname]) > 0.8
    )
    n_flaky = sum(
        1 for tname, bids in week_test_builds.items()
        if len(bids) >= 3
        and week_test_total.get(tname)
        and 0.2 <= len(bids) / len(week_test_total[tname]) <= 0.8
    )
    if n_broken or n_flaky:
        _w(f"- **Tests:** {n_broken} broken, {n_flaky} flaky")
    _w("")

    # ===================================================================
    # Infrastructure vs Code (compact)
    # ===================================================================
    week_step_failures = [s for s in all_steps
                         if s["build_id"] in failed_build_ids and s.get("level") == "Error"]
    infra_count = sum(1 for s in week_step_failures if s.get("is_infra"))
    code_count = len(week_step_failures) - infra_count
    total_steps = len(week_step_failures)

    if total_steps:
        total_wasted_h = sum(
            (build_map[b["build_id"]].get("duration_seconds") or 0) / 3600
            for b in week_builds if b["result"] == "failure" and b["build_id"] in build_map
        )
        _w("## Infrastructure vs Code Failures")
        _w("")
        _w(f"- **Infrastructure:** {infra_count}/{total_steps} step failures "
           f"({infra_count/total_steps*100:.0f}%)")
        _w(f"- **Code/test:** {code_count}/{total_steps} step failures "
           f"({code_count/total_steps*100:.0f}%)")
        _w(f"- **Wasted CI time:** ~{total_wasted_h:.0f}h on failed builds")
        _w("")
        infra_pct = infra_count / total_steps if total_steps else 0
        if infra_pct > 0.75:
            _w("Infrastructure dominates failures this week. "
               "Most retests are caused by cluster provisioning / image build "
               "issues, not code bugs. Investigation required.")
        elif infra_pct > 0.5:
            _w("Infrastructure causes the majority of failures, but code/test "
               "issues are also significant. Address the test breakages below "
               "while investigating infra issues.")
        elif code_count > infra_count:
            _w("Code and test failures are the primary failure source this week. "
               "Focus on the broken tests below — fixing them will have the "
               "biggest impact on CI pass rates.")
        else:
            _w("Infrastructure and code failures are roughly even. "
               "Both need attention.")
        _w("")

    # ===================================================================
    # New Breakages This Week — split by scope
    # ===================================================================
    # Collect new breakages with their regression onset info
    new_breakage_data: list[tuple[str, int, int, dict | None]] = []
    for tname, fail_bids in week_test_builds.items():
        total_b = len(week_test_total.get(tname, set()))
        if total_b < 2:
            continue
        week_rate = len(fail_bids) / total_b

        prev_fail_b = len(prev_test_builds.get(tname, set()))
        prev_total_b = len(prev_test_total.get(tname, set()))
        prev_rate = prev_fail_b / prev_total_b if prev_total_b else 0.0

        if week_rate > 0.5 and prev_rate < 0.2:
            onset = _detect_regression_onset(
                tname, all_test_results, build_map, all_prs, links,
            )
            new_breakage_data.append((tname, len(fail_bids), total_b, onset))

    new_breakage_data.sort(key=lambda x: x[1], reverse=True)

    # Separate into codebase-wide vs PR-specific
    codebase_wide: list[tuple[str, int, int, dict | None]] = []
    pr_specific: list[tuple[str, int, int, dict | None]] = []
    for entry in new_breakage_data:
        tname, fail_b, total_b, onset = entry
        if onset and onset.get("pattern") == "pr_under_test":
            pr_specific.append(entry)
        else:
            codebase_wide.append(entry)

    def _format_breakage(tname: str, fail_b: int, total_b: int,
                         onset: dict | None) -> list[str]:
        """Format a single breakage entry, returning lines to append."""
        out: list[str] = []
        rate = fail_b / total_b if total_b else 0
        short = tname.rsplit("/", 1)[-1] if "/" in tname else tname
        test_file = _test_name_to_file(tname)

        cause_hint = ""
        if onset and onset.get("causal_pr"):
            pr_num = onset["causal_pr"]
            pr_ref = f"#{pr_num}"
            if links:
                pr_ref = f"[#{pr_num}]({links.github_pr(pr_num)})"
            pr_title = onset.get("pr_title", "")[:60]
            if onset["pattern"] == "pr_under_test":
                cause_hint = f" — PR {pr_ref} (*{pr_title}*)"
            else:
                cause_hint = f" — likely caused by {pr_ref} (*{pr_title}*)"

        raw_msg = week_test_msg.get(tname, "")
        msg_hint = ""
        if raw_msg and not _is_wrapper_message(raw_msg):
            msg_hint = f"\n  - Error: `{format_for_table(raw_msg, max_chars=120)}`"

        out.append(f"- **`{short}`** (`{test_file}`) — {rate:.0%} "
                   f"({fail_b}/{total_b}){cause_hint}{msg_hint}")

        if onset:
            pr_files = onset.get("pr_files", [])
            if pr_files:
                file_list = ", ".join(f"`{f}`" for f in pr_files[:4])
                if len(pr_files) > 4:
                    file_list += f" (+{len(pr_files) - 4} more)"
                out.append(f"  - Changed: {file_list}")

        return out

    if codebase_wide:
        _w("## Codebase-Wide Breakages")
        _w("")
        _w("Tests broken **across all PRs** — a merged change broke these "
           "for everyone. These block the entire team.")
        _w("")
        for tname, fail_b, total_b, onset in codebase_wide[:7]:
            for line in _format_breakage(tname, fail_b, total_b, onset):
                _w(line)
        _w("")

    if pr_specific:
        _w("## PR-Specific Failures")
        _w("")
        _w("Tests failing **only in specific PRs** — the PR's own changes "
           "are causing the failure. The PR author needs to fix their code.")
        _w("")
        for tname, fail_b, total_b, onset in pr_specific[:7]:
            for line in _format_breakage(tname, fail_b, total_b, onset):
                _w(line)
        _w("")

    # ===================================================================
    # Resolved This Week
    # ===================================================================
    resolved: list[tuple[str, int, int]] = []
    for tname, prev_bids in prev_test_builds.items():
        prev_total_b = len(prev_test_total.get(tname, set()))
        if prev_total_b < 2:
            continue
        prev_rate = len(prev_bids) / prev_total_b

        week_fail_b = len(week_test_builds.get(tname, set()))
        week_total_b = len(week_test_total.get(tname, set()))
        week_rate = week_fail_b / week_total_b if week_total_b else 0.0

        # Resolved: was failing >50% last week, now <10% or no data
        if prev_rate > 0.5 and week_rate < 0.1:
            resolved.append((tname, len(prev_bids), prev_total_b))

    resolved.sort(key=lambda x: x[1], reverse=True)

    if resolved:
        _w("## Resolved This Week")
        _w("")
        _w("Tests that were failing last week but are now passing:")
        _w("")
        for tname, prev_fail, prev_total in resolved[:5]:
            short = tname.rsplit("/", 1)[-1] if "/" in tname else tname
            _w(f"- `{short}` — was {prev_fail}/{prev_total} "
               f"({prev_fail/prev_total:.0%}), now passing")
        _w("")

    # ===================================================================
    # Ongoing Broken Tests (persistent problems)
    # ===================================================================
    ongoing_broken: list[tuple[str, int, int, float]] = []
    for tname, fail_bids in week_test_builds.items():
        total_b = len(week_test_total.get(tname, set()))
        if total_b < 3:
            continue
        week_rate = len(fail_bids) / total_b
        if week_rate <= 0.8:
            continue

        # Was it also broken last week?
        prev_fail_b = len(prev_test_builds.get(tname, set()))
        prev_total_b = len(prev_test_total.get(tname, set()))
        prev_rate = prev_fail_b / prev_total_b if prev_total_b else 0.0

        if prev_rate > 0.5:
            ongoing_broken.append((tname, len(fail_bids), total_b, prev_rate))

    ongoing_broken.sort(key=lambda x: x[1], reverse=True)

    if ongoing_broken:
        _w("## Still Broken (Codebase-Wide, Ongoing)")
        _w("")
        _w("Tests broken **across all PRs** both this week and last. "
           "These are persistent codebase-wide failures — not caused by "
           "any individual PR this week.")
        _w("")
        _w("| Test | This Week | Last Week | File |")
        _w("|------|-----------|-----------|------|")
        for tname, fail_b, total_b, prev_rate in ongoing_broken[:8]:
            short = tname.rsplit("/", 1)[-1] if "/" in tname else tname
            test_file = _test_name_to_file(tname)
            week_rate = fail_b / total_b
            _w(f"| `{short}` | {week_rate:.0%} ({fail_b}/{total_b}) "
               f"| {prev_rate:.0%} | `{test_file}` |")
        _w("")

    # ===================================================================
    # Flaky Tests (worsening)
    # ===================================================================
    worsening_flakes: list[tuple[str, float, float, int, int]] = []
    for tname, fail_bids in week_test_builds.items():
        total_b = len(week_test_total.get(tname, set()))
        if total_b < 3:
            continue
        week_rate = len(fail_bids) / total_b
        if not (0.2 <= week_rate <= 0.8):
            continue

        prev_fail_b = len(prev_test_builds.get(tname, set()))
        prev_total_b = len(prev_test_total.get(tname, set()))
        prev_rate = prev_fail_b / prev_total_b if prev_total_b else 0.0

        if week_rate > prev_rate + 0.15:
            worsening_flakes.append((tname, week_rate, prev_rate, len(fail_bids), total_b))

    worsening_flakes.sort(key=lambda x: x[1] - x[2], reverse=True)

    if worsening_flakes:
        _w("## Worsening Flakes")
        _w("")
        _w("Tests that became more flaky this week:")
        _w("")
        for tname, wk_rate, prev_rate, fail_b, total_b in worsening_flakes[:5]:
            short = tname.rsplit("/", 1)[-1] if "/" in tname else tname
            test_file = _test_name_to_file(tname)
            delta = wk_rate - prev_rate
            _w(f"- `{short}` (`{test_file}`) — {wk_rate:.0%} this week "
               f"(was {prev_rate:.0%}, +{delta:.0%})")
        _w("")

    # ===================================================================
    # Component Risk Watch (improved — code failures only)
    # ===================================================================
    comp_builds: dict[str, list[dict]] = defaultdict(list)
    for b in week_builds:
        for comp in pr_components.get(b["pr_number"], ["unknown"]):
            comp_builds[comp].append(b)

    # Count per-component test failures — only attribute a test to a
    # component if the test name actually relates to that component (e.g.
    # a kserve test failure shouldn't be counted against datasciencepipelines
    # just because a DSP PR ran the full e2e suite).
    comp_test_fails: dict[str, int] = Counter()
    comp_test_own_fails: dict[str, int] = Counter()
    for t in week_test_failures:
        tname_lower = t["test_name"].lower()
        binfo = build_map.get(t["build_id"])
        if binfo:
            pr_comps = pr_components.get(binfo["pr_number"], ["unknown"])
            for comp in pr_comps:
                comp_test_fails[comp] += 1
                # "Own" failure: the test name contains this component
                comp_slug = comp.lower().replace("-", "").replace("_", "")
                if len(comp_slug) > 3 and comp_slug in tname_lower.replace("-", "").replace("_", ""):
                    comp_test_own_fails[comp] += 1

    if comp_builds:
        # Only show components with actual code/test failures or high build volume
        rows: list[tuple[str, int, float, float, int, int, str]] = []
        for comp, cbuilds in sorted(comp_builds.items()):
            summary = ci_efficiency.compute_summary(cbuilds)
            fail_rate = summary.get("cycle_failure_rate") or 0.0

            comp_failed_bids = {b["build_id"] for b in cbuilds if b["result"] == "failure"}
            infra_fails = sum(1 for s in all_steps
                             if s["build_id"] in comp_failed_bids
                             and s.get("is_infra") and s.get("level") == "Error")
            code_fails = sum(1 for s in all_steps
                            if s["build_id"] in comp_failed_bids
                            and not s.get("is_infra") and s.get("level") == "Error")
            test_fails = comp_test_fails.get(comp, 0)

            own_fails = comp_test_own_fails.get(comp, 0)

            # Risk based on component-specific test failures, not inherited
            # failures from the shared e2e suite
            if own_fails > 3:
                risk = "HIGH"
            elif own_fails > 0:
                risk = "MEDIUM"
            elif fail_rate > 0.5:
                risk = "infra"
            else:
                risk = "low"

            rt_val = summary.get("retest_tax") or 0
            rows.append((comp, len(cbuilds), fail_rate, rt_val,
                         infra_fails, own_fails, risk))

        # Sort: HIGH first, then MEDIUM, show interesting components
        priority = {"HIGH": 0, "MEDIUM": 1, "infra": 2, "low": 3}
        rows.sort(key=lambda r: (priority.get(r[6], 3), -r[5]))

        _w("## Component Health")
        _w("")
        _w("| Component | Builds | Fail% | Test Fails | Infra Fails | Risk |")
        _w("|-----------|--------|-------|------------|-------------|------|")
        for comp, n_builds, fail_rate, rt_val, infra_f, test_f, risk in rows:
            if risk == "low" and n_builds < 20:
                continue
            risk_label = f"**{risk.upper()}**" if risk in ("HIGH", "MEDIUM") else risk
            _w(f"| {comp} | {n_builds} | {_fmt_pct(fail_rate)} "
               f"| {test_f} | {infra_f} | {risk_label} |")
        _w("")

        high_risk = [r[0] for r in rows if r[6] == "HIGH"]
        if high_risk:
            _w(f"**Action needed:** {', '.join(high_risk)} "
               f"{'has' if len(high_risk) == 1 else 'have'} "
               f"significant code/test failures this week.")
            _w("")

    # ===================================================================
    # Revert Watch
    # ===================================================================
    if week_reverts:
        _w("## Revert Watch")
        _w("")
        for r in week_reverts:
            pr_num = r.get("reverted_pr")
            pr_label = ""
            if pr_num and links:
                pr_label = f" ([PR #{pr_num}]({links.github_pr(pr_num)}))"
            elif pr_num:
                pr_label = f" (PR #{pr_num})"
            _w(f"- {r['date']}: `{r['sha'][:12]}`{pr_label} — {r.get('message', '')[:80]}")
        _w("")

    # ===================================================================
    # CI Duration
    # ===================================================================
    dur = week_summary.get("cycle_duration_minutes", {})
    prev_dur = prev_summary.get("cycle_duration_minutes", {})
    if dur.get("count", 0) > 0:
        _w("## CI Duration")
        _w("")
        _w(f"- **Median cycle time:** {dur.get('p50', 0):.0f} minutes")
        _w(f"- **P90 cycle time:** {dur.get('p90', 0):.0f} minutes")
        ci_hrs = week_summary.get("ci_hours_per_pr", {})
        if ci_hrs.get("mean"):
            _w(f"- **Avg CI wait per PR:** {ci_hrs['mean']:.1f} hours")

        if prev_dur.get("p50") and dur.get("p50"):
            diff = dur["p50"] - prev_dur["p50"]
            if abs(diff) >= 5:
                direction = "slower" if diff > 0 else "faster"
                _w(f"- **Trend:** {abs(diff):.0f} min {direction} than previous week")
        _w("")

    # ===================================================================
    # AI-Assisted Commits
    # ===================================================================
    ai_prs = [p for p in week_prs if p.get("is_ai_assisted")]
    if ai_prs:
        _w("## AI-Assisted Commits")
        _w("")
        _w(f"- **{len(ai_prs)} of {len(week_prs)} PRs** ({len(ai_prs)/len(week_prs)*100:.0f}%) "
           f"this week had AI-assisted commits")
        ai_builds = [b for b in week_builds if b["pr_number"] in {p["number"] for p in ai_prs}]
        if ai_builds:
            ai_summary = ci_efficiency.compute_summary(ai_builds)
            ai_fr = ai_summary.get("cycle_failure_rate")
            _w(f"- **AI-PR failure rate:** {_fmt_pct(ai_fr)} (vs {_fmt_pct(fr)} overall)")
        _w("")

    # ===================================================================
    # JIRA Context
    # ===================================================================
    jira_issue_map = store.get_jira_issue_map()
    if jira_issue_map:
        week_jira_keys: set[str] = set()
        for p in week_prs:
            week_jira_keys.update(_parse_json_field(p.get("jira_keys")))

        week_jira_issues = [jira_issue_map[k] for k in week_jira_keys if k in jira_issue_map]
        if week_jira_issues:
            type_counts: Counter[str] = Counter()
            priority_counts: Counter[str] = Counter()
            blockers: list[dict] = []

            for issue in week_jira_issues:
                type_counts[issue.get("issue_type") or "Unknown"] += 1
                prio = issue.get("priority") or "Unknown"
                priority_counts[prio] += 1
                if prio in ("Blocker", "Critical") and issue.get("status_category") != "Done":
                    blockers.append(issue)

            _w("## JIRA Context")
            _w("")
            type_parts = [f"{cnt} {t}" for t, cnt in type_counts.most_common()]
            _w(f"- **Issue types this week:** {', '.join(type_parts)}")
            prio_parts = [f"{cnt} {p}" for p, cnt in priority_counts.most_common()]
            _w(f"- **Priorities:** {', '.join(prio_parts)}")

            if blockers:
                _w(f"- **{len(blockers)} Blocker/Critical issues still open:**")
                for b in blockers[:5]:
                    _w(f"  - [{b['key']}] {b.get('summary', '')[:60]} "
                       f"({b.get('status', 'Unknown')})")
            _w("")

    # ===================================================================
    # Release Activity
    # ===================================================================
    releases = store.get_releases()
    recent_releases = [r for r in releases if (r.get("published") or "") >= cutoff_str]
    cherry_picks = store.get_cherry_picks()
    week_cps = [c for c in cherry_picks if (c.get("merged_at") or "") >= cutoff_str]

    if recent_releases or week_cps:
        _w("## Release Activity")
        _w("")
        if recent_releases:
            for r in recent_releases:
                tag_type = "EA" if r.get("is_ea") else ("patch" if r.get("is_patch") else "stable")
                _w(f"- **{r['tag']}** ({tag_type}) published {r['published']}")
        if week_cps:
            _w(f"- **{len(week_cps)} cherry-picks** merged to downstream branches")
        _w("")

    _w("---")
    _w(f"*Generated {now.strftime('%Y-%m-%d %H:%M UTC')} from {len(all_builds)} total CI builds "
       f"across {len(all_prs)} PRs.*")
    _w("")

    return "\n".join(lines)
