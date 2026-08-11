"""Recurring failure pattern analyzer.

Clusters similar error messages, computes flake rates per step and component,
identifies persistent vs transient failures, and surfaces root-cause signals
that an AI agent or human operator can act on.

Each error cluster is enriched with temporal context (first/last seen, trend),
affected CI steps and PRs, changed file paths, code risk hotspots, and an
actionability classification so agents can triage without reading raw logs.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from reports.assertion_parser import format_for_report, format_for_table, parse_failure_message

from metrics import ci_efficiency
from reports.links import LinkBuilder, local_access_appendix
from store.db import Store

log = logging.getLogger(__name__)

MANIFEST_FILE_PATTERNS = (
    "get_all_manifests.sh",
    "build/operands-map.yaml",
)

MANIFEST_TITLE_PATTERNS = [
    re.compile(r"update manifest", re.IGNORECASE),
    re.compile(r"bump .* image", re.IGNORECASE),
    re.compile(r"chore.*manifest.*sha", re.IGNORECASE),
    re.compile(r"chore\(deps\)", re.IGNORECASE),
]


def _is_manifest_update_pr(pr: dict, changed_files: list[str] | None = None) -> bool:
    """Detect whether a PR is an automated manifest or image update."""
    title = pr.get("title") or ""
    if any(pat.search(title) for pat in MANIFEST_TITLE_PATTERNS):
        return True
    files = changed_files if changed_files is not None else _parse_json_field(pr.get("changed_files"))
    return any(
        f in MANIFEST_FILE_PATTERNS or f.endswith("/params.env") or "kustomization" in f.lower()
        for f in files
    )


def _detect_manifest_regressions(
    manifest_prs: list[dict],
    all_builds: list[dict],
    all_steps: list[dict],
    build_start_map: dict[str, str],
) -> list[dict]:
    """Detect test steps whose failure rate spiked after a manifest update.

    For each recent manifest-update PR, compares the per-step failure rate
    in the 14 days before vs 14 days after it merged.  Returns regressions
    sorted by severity (absolute failure-rate increase).

    ``build_start_map`` maps build_id → started_at timestamp so we can
    bucket builds into before/after windows without re-scanning the list.
    """
    sorted_mprs = sorted(
        [p for p in manifest_prs if p.get("merged_at")],
        key=lambda p: p["merged_at"],
        reverse=True,
    )
    if not sorted_mprs:
        return []

    step_by_build: dict[str, list[dict]] = defaultdict(list)
    for s in all_steps:
        step_by_build[s["build_id"]].append(s)

    regressions: list[dict] = []
    seen_steps: set[str] = set()

    for mpr in sorted_mprs[:5]:
        merge_ts = mpr["merged_at"]

        before_bids: set[str] = set()
        after_bids: set[str] = set()
        for b in all_builds:
            ts = build_start_map.get(b["build_id"]) or b.get("started_at") or ""
            if not ts:
                continue
            if ts < merge_ts:
                before_bids.add(b["build_id"])
            else:
                after_bids.add(b["build_id"])

        if len(after_bids) < 3:
            continue

        n_before = len(before_bids)
        n_after = len(after_bids)

        before_fail: dict[str, int] = defaultdict(int)
        after_fail: dict[str, int] = defaultdict(int)

        for bid in before_bids:
            for s in step_by_build.get(bid, []):
                if s.get("level") == "Error":
                    before_fail[s["step_name"]] += 1

        for bid in after_bids:
            for s in step_by_build.get(bid, []):
                if s.get("level") == "Error":
                    after_fail[s["step_name"]] += 1

        for step_name, af in after_fail.items():
            if step_name in seen_steps or af < 2:
                continue
            bf = before_fail.get(step_name, 0)
            before_rate = bf / n_before if n_before else 0.0
            after_rate = af / n_after if n_after else 0.0

            increase = after_rate - before_rate
            if increase > 0.15 and after_rate > 0.25:
                seen_steps.add(step_name)
                regressions.append({
                    "step": step_name,
                    "manifest_pr": mpr,
                    "before_rate": before_rate,
                    "after_rate": after_rate,
                    "after_failures": af,
                    "after_total": n_after,
                    "increase": increase,
                    "is_infra": any(
                        s.get("is_infra")
                        for bid in after_bids
                        for s in step_by_build.get(bid, [])
                        if s["step_name"] == step_name and s.get("level") == "Error"
                    ),
                })

    regressions.sort(key=lambda r: r["increase"], reverse=True)
    return regressions


_NON_CODE_EXTENSIONS = frozenset({
    ".md", ".txt", ".rst", ".yml", ".yaml", ".json", ".toml",
    ".png", ".jpg", ".svg", ".gif",
})


def _pr_relevance_to_test(pr: dict, test_name: str) -> int:
    """Score how likely a PR's changes could affect the given test.

    Higher score = more likely causal.  Considers component overlap and
    whether the PR touches actual code vs documentation/CI config.
    """
    score = 0
    files = _parse_json_field(pr.get("changed_files"))
    comps = _parse_json_field(pr.get("changed_components"))

    test_lower = test_name.lower()

    # Component match: if the test name contains a component the PR changed
    for comp in comps:
        if comp.lower() in test_lower:
            score += 50

    # File-path heuristic: Go source in relevant directories
    has_code = False
    for f in files:
        ext = f[f.rfind("."):] if "." in f else ""
        if ext in _NON_CODE_EXTENSIONS:
            continue
        has_code = True

        f_lower = f.lower()
        # Operator core (controller, pkg, internal, api)
        if any(d in f_lower for d in ("internal/", "pkg/", "api/", "cmd/")):
            score += 10
        # Test infra
        if "tests/" in f_lower or "_test.go" in f_lower:
            score += 5
        # Config / manifests
        if "config/" in f_lower or "manifest" in f_lower:
            score += 8

        # Component name in the file path
        parts = test_name.split("/")
        for part in parts:
            slug = part.lower().replace("_", "").replace("-", "")
            if len(slug) > 3 and slug in f_lower.replace("_", "").replace("-", ""):
                score += 20
                break

    # Penalty if the PR only touches non-code files
    if files and not has_code:
        score = max(score - 30, 0)

    # Recency bonus is implicit (handled by caller sorting by merge time)
    return score


def _detect_regression_onset(
    test_name: str,
    all_test_results: list[dict],
    build_map: dict[str, dict],
    merged_prs: list[dict],
    links: "LinkBuilder | None" = None,
) -> dict | None:
    """Identify when a test started failing and what likely caused it.

    Returns a dict with:
      onset_date   – ISO timestamp of the first failure
      pattern      – "pr_under_test" or "merged_to_main"
      causal_pr    – PR number most likely responsible
      pr_title     – title of the causal PR
      pr_files     – changed files in the causal PR
      confidence   – "high" / "medium" / "low"
    """
    # Collect (timestamp, status, pr_number) tuples for this test
    timeline: list[tuple[str, str, int]] = []
    for t in all_test_results:
        if t["test_name"] != test_name or not t.get("is_leaf"):
            continue
        binfo = build_map.get(t["build_id"])
        if not binfo or not binfo.get("started_at"):
            continue
        timeline.append((binfo["started_at"], t["status"], binfo["pr_number"]))

    if not timeline:
        return None

    timeline.sort(key=lambda x: x[0])

    # Find the transition: last pass -> first fail
    last_pass_ts = None
    first_fail_ts = None
    first_fail_pr = None
    for ts, status, pr_num in timeline:
        if status != "failed":
            last_pass_ts = ts
        elif first_fail_ts is None:
            first_fail_ts = ts
            first_fail_pr = pr_num

    if first_fail_ts is None:
        return None

    # Count failures per PR to detect dominance
    pr_fail_count: Counter = Counter()
    total_fails = 0
    for ts, status, pr_num in timeline:
        if status == "failed":
            pr_fail_count[pr_num] += 1
            total_fails += 1

    failing_prs = set(pr_fail_count.keys())
    dominant_pr, dominant_count = pr_fail_count.most_common(1)[0]
    dominant_ratio = dominant_count / total_fails if total_fails else 0

    result: dict = {
        "onset_date": first_fail_ts,
        "last_pass_date": last_pass_ts,
    }

    # Pattern 1: one PR dominates (>60% of failures) → it's the PR under test
    if dominant_ratio > 0.6 or len(failing_prs) <= 2:
        causal_pr_num = dominant_pr if dominant_ratio > 0.6 else first_fail_pr
        result["pattern"] = "pr_under_test"
        result["confidence"] = "high" if dominant_ratio > 0.8 else "medium"

        pr_info = next((p for p in merged_prs if p["number"] == causal_pr_num), None)
        if pr_info:
            result["causal_pr"] = causal_pr_num
            result["pr_title"] = pr_info.get("title", "")
            result["pr_files"] = _parse_json_field(pr_info.get("changed_files"))
        else:
            result["causal_pr"] = causal_pr_num
            result["pr_title"] = "(PR not yet merged — failures are in its CI runs)"
            result["pr_files"] = []
    else:
        # Pattern 2: many different PRs fail → something merged to main broke it
        result["pattern"] = "merged_to_main"

        # Gather candidate PRs merged before the first failure (last 48h window)
        try:
            onset_dt = datetime.fromisoformat(
                first_fail_ts.replace("Z", "+00:00"))
            window_start = (onset_dt - timedelta(hours=48)).isoformat()
        except (ValueError, TypeError):
            window_start = ""

        candidates = [
            p for p in merged_prs
            if p.get("merged_at") and p["merged_at"] < first_fail_ts
            and (not window_start or p["merged_at"] >= window_start)
            and p.get("base_branch") == "main"
        ]

        if not candidates:
            # Widen to any PR merged before onset
            candidates = [
                p for p in merged_prs
                if p.get("merged_at") and p["merged_at"] < first_fail_ts
                and p.get("base_branch") == "main"
            ]

        # Rank by relevance to the test, then by recency (tiebreaker)
        candidates.sort(
            key=lambda p: (
                _pr_relevance_to_test(p, test_name),
                p.get("merged_at", ""),
            ),
            reverse=True,
        )

        if candidates:
            suspect = candidates[0]
            result["causal_pr"] = suspect["number"]
            result["pr_title"] = suspect.get("title", "")
            result["pr_files"] = _parse_json_field(suspect.get("changed_files"))
            relevance = _pr_relevance_to_test(suspect, test_name)
            result["confidence"] = "high" if relevance >= 40 else "medium"

            # Include runners-up with non-trivial relevance
            runners_up = []
            for p in candidates[1:4]:
                if _pr_relevance_to_test(p, test_name) > 0:
                    runners_up.append(p)
            if runners_up:
                result["runners_up"] = [
                    {"number": p["number"], "title": p.get("title", "")}
                    for p in runners_up
                ]
        else:
            result["confidence"] = "low"
            result["causal_pr"] = None
            result["pr_title"] = "(no merged PR found before onset)"
            result["pr_files"] = []

    return result


def _parse_json_field(value: str | None) -> list:
    if not value:
        return []
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []


_WRAPPER_PATTERNS = [
    re.compile(r"testing\.go:\d+: test executed panic\(nil\)"),
    re.compile(r"^panic: test executed panic\(nil\)"),
    re.compile(r"subtest may have called FailNow on a parent test"),
    re.compile(r"^FAIL\s*$"),
    re.compile(r"^exit status \d+\s*$"),
    re.compile(r"^panic: test timed out after"),
    re.compile(r"^goroutine \d+ \[running\]"),
    re.compile(r"^signal: killed$"),
]


def _is_wrapper_message(msg: str) -> bool:
    """Detect Go test framework wrapper messages that hide the real error.

    These messages indicate *that* a test failed but not *why*. The actual
    root cause is in a child subtest's failure message or in the raw test
    output (stdout/stderr).

    Checks the first several meaningful lines because JUnit failure bodies
    often start with ``=== RUN`` or ``--- FAIL`` markers before the wrapper.
    """
    if not msg:
        return False
    for line in msg.split("\n")[:10]:
        stripped = line.strip()
        if not stripped or stripped.startswith("=== RUN") or stripped.startswith("--- FAIL"):
            continue
        return any(pat.search(stripped) for pat in _WRAPPER_PATTERNS)
    return False


_NOISE_PATTERNS = [
    re.compile(r"\b(0x[0-9a-f]+|[0-9a-f]{8,})\b", re.IGNORECASE),
    re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[^\s]*"),
    re.compile(r"\b\d+\.\d+\.\d+\.\d+(:\d+)?\b"),
    re.compile(r"\b[a-f0-9]{12,}\b"),
    re.compile(r"namespace/[\w-]+"),
    re.compile(r"pod/[\w-]+"),
]


def _normalize_message(msg: str) -> str:
    """Collapse variable parts of error messages for clustering."""
    normalized = msg
    for pat in _NOISE_PATTERNS:
        normalized = pat.sub("*", normalized)
    normalized = re.sub(r"\*(\*)+", "*", normalized)
    return normalized.strip()[:200]


def _compute_flake_rate(builds: list[dict]) -> dict:
    """Compute flake rate: PRs that had both pass and fail cycles.

    High flake rate = failures are non-deterministic (infra or timing).
    Low flake rate = failures are consistent (real code bugs).
    """
    pr_builds: dict[int, list[dict]] = defaultdict(list)
    for b in builds:
        pr_builds[b["pr_number"]].append(b)

    total_prs = len(pr_builds)
    flaky_prs = 0
    consistent_fail_prs = 0
    clean_prs = 0

    for pr_num, blist in pr_builds.items():
        blist.sort(key=lambda x: x["build_id"])
        cycles = ci_efficiency._derive_cycles(blist)
        results = {c["result"] for c in cycles}
        if "success" in results and "failure" in results:
            flaky_prs += 1
        elif results == {"failure"}:
            consistent_fail_prs += 1
        else:
            clean_prs += 1

    return {
        "total_prs": total_prs,
        "flaky_prs": flaky_prs,
        "consistent_fail_prs": consistent_fail_prs,
        "clean_prs": clean_prs,
        "flake_rate": round(flaky_prs / total_prs, 3) if total_prs else None,
    }


def _classify_action(*, is_infra: bool, flake_rate: float | None,
                     consistent_fail_prs: int, total_prs: int,
                     trend: str, n_builds: int) -> str:
    """Decide what an agent should do with this error cluster."""
    if is_infra:
        if trend == "worsening" or n_builds > 50:
            return "investigate_infra"
        if trend == "resolved":
            return "monitor"
        return "report_infra"
    if consistent_fail_prs > 0 and total_prs > 0:
        if consistent_fail_prs / total_prs > 0.4:
            return "fix_test"
    if flake_rate is not None and flake_rate > 0.7:
        return "stabilize_flake"
    if flake_rate is not None and flake_rate > 0.3:
        return "stabilize"
    return "investigate"


_ACTION_LABELS = {
    "investigate_infra": (
        "Investigate infrastructure — this infra failure is high-impact or worsening. "
        "Check cluster pool health, quotas, and IPI install logs. "
        "File an upstream issue if the root cause is in shared CI infrastructure."
    ),
    "report_infra": (
        "Report infrastructure — recurring infra flake. "
        "Retesting works around it, but track the trend and report upstream "
        "if it persists. Document in known-patterns so the team stops re-investigating."
    ),
    "monitor": (
        "Monitor — this issue appears resolved (no recent occurrences). "
        "Keep watching for recurrence."
    ),
    "fix_test": (
        "Fix test — consistent failures suggest a real test or code bug. "
        "Look at the error messages and affected files below."
    ),
    "stabilize_flake": (
        "Stabilize — high flake rate (>70%). Add retry logic, increase timeouts, "
        "or improve test isolation. Don't change assertions."
    ),
    "stabilize": (
        "Stabilize — moderate flakiness. Improve test isolation or add retries "
        "to reduce CI waste."
    ),
    "investigate": (
        "Investigate — gather more evidence. Check the logs and affected PRs "
        "to determine if this is a code bug, flake, or infra issue."
    ),
}


def _weekly_trend(build_dates: list[str]) -> str:
    """Compare the last 14 days vs the 14 days before to detect trajectory."""
    if len(build_dates) < 2:
        return "insufficient data"
    now = datetime.now(timezone.utc)
    recent_cutoff = (now - timedelta(days=14)).strftime("%Y-%m-%d")
    prev_cutoff = (now - timedelta(days=28)).strftime("%Y-%m-%d")
    recent = sum(1 for d in build_dates if d >= recent_cutoff)
    prev = sum(1 for d in build_dates if prev_cutoff <= d < recent_cutoff)
    if recent == 0 and prev == 0:
        oldest = build_dates[0][:10] if build_dates else ""
        if oldest and oldest < prev_cutoff:
            return "resolved"
        return "insufficient data"
    if recent == 0 and prev > 0:
        return "resolved"
    if prev == 0 and recent > 0:
        return "new (last 2 weeks)"
    if recent > prev * 1.5:
        return "worsening"
    if recent < prev * 0.5:
        return "improving"
    return "stable"


_COMPONENT_FILE_MAP = {
    "auth": "authcontroller_test.go",
    "cfmap_deletion": "cfmap_deletion_test.go",
    "modelcontroller": "modelcontroller_test.go",
    "odh_manager": "odh_manager_test.go",
}


def _test_name_to_file(test_name: str) -> str:
    """Map a Go/JUnit test name to its likely source file in tests/e2e/.

    Handles patterns like:
      TestOdhOperator/components/group_1/kserve/Validate... -> tests/e2e/kserve_test.go
      TestOdhOperator/services/group_1/monitoring/Test...   -> tests/e2e/monitoring_test.go
      Operator_Manager_E2E_Tests/...                        -> tests/e2e/odh_manager_test.go
    """
    parts = test_name.replace(" ", "_").split("/")

    for i, p in enumerate(parts):
        if p.startswith("group_") and i + 1 < len(parts):
            comp = parts[i + 1].lower()
            fname = _COMPONENT_FILE_MAP.get(comp, f"{comp}_test.go")
            return f"tests/e2e/{fname}"

    lower = test_name.lower()
    if "deletion_configmap" in lower or "cfmap" in lower:
        return "tests/e2e/cfmap_deletion_test.go"
    if "deletion" in lower:
        return "tests/e2e/deletion_test.go"
    if "operator_manager" in lower or "odh_manager" in lower:
        return "tests/e2e/odh_manager_test.go"
    if "resilience" in lower:
        return "tests/e2e/resilience_test.go"
    if "dscinitial" in lower or "dsc_management" in lower or "creation" in lower:
        return "tests/e2e/creation_test.go"
    if "v2tov3" in lower or "upgrade" in lower:
        return "tests/e2e/v2tov3upgrade_test.go"

    return "tests/e2e/"


def generate(store: Store, lookback_days: int = 30,
             links: LinkBuilder | None = None) -> str:
    """Generate a recurring failure pattern report."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    cutoff_str = cutoff.strftime("%Y-%m-%d")

    all_prs = store.get_merged_prs(base_branch="main")
    period_prs = [p for p in all_prs if (p.get("merged_at") or "") >= cutoff_str]
    period_pr_nums = {p["number"] for p in period_prs}

    all_builds = store.get_ci_builds()
    period_builds = [b for b in all_builds if b["pr_number"] in period_pr_nums]
    failed_builds = [b for b in period_builds if b["result"] == "failure"]
    failed_build_ids = {b["build_id"] for b in failed_builds}

    all_steps = store.get_all_build_steps()
    all_fail_msgs = store.get_all_build_failure_messages()
    code_risks = store.get_code_risk_scores()

    pr_map: dict[int, dict] = {p["number"]: p for p in period_prs}
    pr_components: dict[int, list[str]] = {}
    pr_files: dict[int, list[str]] = {}
    for p in period_prs:
        comps = _parse_json_field(p.get("changed_components"))
        if comps:
            pr_components[p["number"]] = comps
        files = _parse_json_field(p.get("changed_files"))
        if files:
            pr_files[p["number"]] = files

    build_map: dict[str, dict] = {b["build_id"]: b for b in period_builds}
    build_to_pr: dict[str, int] = {b["build_id"]: b["pr_number"] for b in period_builds}

    step_by_build: dict[str, list[dict]] = defaultdict(list)
    for s in all_steps:
        if s["build_id"] in failed_build_ids:
            step_by_build[s["build_id"]].append(s)

    lines: list[str] = []
    _w = lines.append

    # ===================================================================
    # Data prep: steps, tests, infra classification
    # ===================================================================
    period_step_failures = [s for s in all_steps
                           if s["build_id"] in failed_build_ids and s.get("level") == "Error"]
    step_counter: Counter[str] = Counter()
    step_infra: dict[str, bool] = {}
    for s in period_step_failures:
        step_counter[s["step_name"]] += 1
        step_infra[s["step_name"]] = bool(s.get("is_infra"))

    infra_step_builds = sum(cnt for step, cnt in step_counter.items()
                            if step_infra.get(step))
    code_step_builds = sum(cnt for step, cnt in step_counter.items()
                           if not step_infra.get(step))

    total_wasted = sum(
        (build_map[b["build_id"]].get("duration_seconds") or 0) / 3600
        for b in failed_builds if b["build_id"] in build_map
    )
    infra_bids = {s["build_id"] for s in period_step_failures if s.get("is_infra")}
    infra_wasted = sum(
        (build_map[bid].get("duration_seconds") or 0) / 3600
        for bid in infra_bids if bid in build_map
    )

    all_test_results = store.get_all_test_results()
    period_test_failures = [
        t for t in all_test_results
        if t["build_id"] in failed_build_ids
        and t["status"] == "failed"
        and t.get("is_leaf")
    ]

    # Build test data structures
    test_fail_counter: Counter[str] = Counter()
    test_build_sets: dict[str, set[str]] = defaultdict(set)
    test_variants: dict[str, set[str]] = defaultdict(set)
    test_sample_msg: dict[str, str] = {}
    test_sample_bid: dict[str, str] = {}
    test_pr_sets: dict[str, set[int]] = defaultdict(set)
    for t in period_test_failures:
        tname = t["test_name"]
        test_fail_counter[tname] += 1
        test_build_sets[tname].add(t["build_id"])
        pr_num = build_to_pr.get(t["build_id"])
        if pr_num:
            test_pr_sets[tname].add(pr_num)
        if t.get("test_variant"):
            test_variants[tname].add(t["test_variant"])
        if t.get("failure_message") and tname not in test_sample_msg:
            test_sample_msg[tname] = t["failure_message"]
        if tname not in test_sample_bid:
            test_sample_bid[tname] = t["build_id"]

    all_test_by_name: dict[str, set[str]] = defaultdict(set)
    for t in all_test_results:
        if t.get("is_leaf"):
            all_test_by_name[t["test_name"]].add(t["build_id"])

    flake = _compute_flake_rate(period_builds)

    # ===================================================================
    # Report header + summary
    # ===================================================================
    _w("# CI Failure Analysis")
    _w(f"**Last {lookback_days} days** (since {cutoff_str})")
    _w("")
    _w(f"- **{len(failed_builds)}/{len(period_builds)} builds failed** across "
       f"{len(period_prs)} PRs")
    flake_pct = (flake["flake_rate"] or 0) * 100
    _w(f"- **Flake rate:** {flake_pct:.0f}% of PRs hit non-deterministic failures")
    n_broken = sum(1 for tname in test_fail_counter
                   if len(test_build_sets[tname]) >= 3
                   and len(all_test_by_name.get(tname, set())) > 0
                   and len(test_build_sets[tname]) / len(all_test_by_name[tname]) > 0.8)
    n_flaky = sum(1 for tname in test_fail_counter
                  if len(test_build_sets[tname]) >= 3
                  and len(all_test_by_name.get(tname, set())) > 0
                  and 0.2 <= len(test_build_sets[tname]) / len(all_test_by_name[tname]) <= 0.8)
    _w(f"- **{n_broken} broken test(s), {n_flaky} flaky test(s)**, "
       f"{len(test_fail_counter)} distinct tests failing")
    _w(f"- **CI waste:** ~{total_wasted:.0f}h total — "
       f"{infra_wasted:.0f}h infrastructure, {total_wasted - infra_wasted:.0f}h code/test")
    _w("")

    # ===================================================================
    # Section 1: BROKEN TESTS (actionable — fix these)
    # ===================================================================
    broken_tests = [
        (tname, len(test_build_sets[tname]), len(all_test_by_name.get(tname, set())))
        for tname in test_fail_counter
        if len(test_build_sets[tname]) >= 3
        and len(all_test_by_name.get(tname, set())) > 0
        and len(test_build_sets[tname]) / len(all_test_by_name[tname]) > 0.8
    ]
    broken_tests.sort(key=lambda x: x[1], reverse=True)

    flaky_tests = [
        (tname, len(test_build_sets[tname]), len(all_test_by_name.get(tname, set())))
        for tname in test_fail_counter
        if len(test_build_sets[tname]) >= 3
        and len(all_test_by_name.get(tname, set())) > 0
        and 0.2 <= len(test_build_sets[tname]) / len(all_test_by_name[tname]) <= 0.8
    ]
    flaky_tests.sort(key=lambda x: x[1], reverse=True)

    if broken_tests:
        _w("## Broken Tests — Fix These First")
        _w("")
        _w("These tests fail in **>80% of builds** they run in. They are "
           "almost certainly broken, not flaky.")
        _w("")

        for i, (tname, fail_b, total_b) in enumerate(broken_tests[:10], 1):
            rate = fail_b / total_b if total_b else 0
            test_file = _test_name_to_file(tname)
            variants = sorted(test_variants.get(tname, set()))

            _w(f"### {i}. `{tname}`")
            _w("")
            _w(f"- **Fail rate:** {fail_b}/{total_b} builds ({rate:.0%})")
            _w(f"- **File:** `{test_file}`")
            if variants:
                _w(f"- **Variants:** {', '.join(variants)}")

            # Regression onset detection
            onset = _detect_regression_onset(
                tname, all_test_results, build_map, all_prs, links,
            )
            if onset:
                onset_date = onset["onset_date"][:10]
                causal_pr = onset.get("causal_pr")
                pattern = onset.get("pattern", "")
                confidence = onset.get("confidence", "low")

                if pattern == "pr_under_test" and causal_pr:
                    pr_ref = f"#{causal_pr}"
                    if links:
                        pr_ref = f"[#{causal_pr}]({links.github_pr(causal_pr)})"
                    _w(f"- **Regression source:** {pr_ref} — "
                       f"this PR's own changes cause the failure "
                       f"(confidence: {confidence})")
                    pr_title = onset.get("pr_title", "")
                    if pr_title and "not yet merged" not in pr_title:
                        _w(f"  - PR title: *{pr_title[:120]}*")
                    pr_files = onset.get("pr_files", [])
                    if pr_files:
                        file_list = ", ".join(f"`{f}`" for f in pr_files[:5])
                        if len(pr_files) > 5:
                            file_list += f" (+{len(pr_files) - 5} more)"
                        _w(f"  - Changed files: {file_list}")

                elif pattern == "merged_to_main" and causal_pr:
                    pr_ref = f"#{causal_pr}"
                    if links:
                        pr_ref = f"[#{causal_pr}]({links.github_pr(causal_pr)})"
                    _w(f"- **Regression source:** Likely {pr_ref} merged to main "
                       f"(failures started {onset_date} across "
                       f"{len(test_pr_sets.get(tname, set()))} PRs, "
                       f"confidence: {confidence})")
                    pr_title = onset.get("pr_title", "")
                    if pr_title:
                        _w(f"  - PR title: *{pr_title[:120]}*")
                    pr_files = onset.get("pr_files", [])
                    if pr_files:
                        file_list = ", ".join(f"`{f}`" for f in pr_files[:5])
                        if len(pr_files) > 5:
                            file_list += f" (+{len(pr_files) - 5} more)"
                        _w(f"  - Changed files: {file_list}")
                    runners = onset.get("runners_up", [])
                    if runners:
                        runner_refs = []
                        for r in runners:
                            ref = f"#{r['number']}"
                            if links:
                                ref = f"[#{r['number']}]({links.github_pr(r['number'])})"
                            runner_refs.append(f"{ref} (*{r['title'][:60]}*)")
                        _w(f"  - Also merged nearby: {'; '.join(runner_refs)}")

                if onset.get("last_pass_date"):
                    _w(f"  - Last passing: {onset['last_pass_date'][:10]}")

            raw_msg = test_sample_msg.get(tname, "")
            if raw_msg:
                if _is_wrapper_message(raw_msg):
                    _w(f"- **Error:** (Go framework wrapper — real error is in test output)")
                    _w(f"  Use `ci-query all-logs <build_id>` or check GCS artifacts "
                       f"to find the actual assertion failure.")
                else:
                    parsed = parse_failure_message(raw_msg)
                    _w(f"- **Error:** {format_for_report(raw_msg)}")
                    if parsed.expected:
                        _w(f"  - Expected: `{parsed.expected[:150]}`")
                    if parsed.root_cause:
                        _w(f"  - Root cause: `{parsed.root_cause[:200]}`")
            if links:
                link_parts = []
                bid = test_sample_bid.get(tname)
                if bid:
                    tests_url = links.ci_obs_tests(bid)
                    logs_url = links.ci_obs_logs(bid)
                    if tests_url:
                        link_parts.append(f"[test results]({tests_url})")
                    if logs_url:
                        link_parts.append(f"[build logs]({logs_url})")
                    binfo = build_map.get(bid)
                    if binfo:
                        gcs_url = links.gcs_artifacts(
                            binfo["pr_number"], binfo["job_name"], bid)
                        link_parts.append(f"[GCS artifacts]({gcs_url})")
                if link_parts:
                    _w(f"- **Investigate:** {' · '.join(link_parts)}")
                affected_prs = sorted(test_pr_sets.get(tname, set()), reverse=True)
                if affected_prs:
                    pr_refs = [f"[#{n}]({links.github_pr(n)})" for n in affected_prs[:5]]
                    _w(f"- **Example PRs:** {', '.join(pr_refs)}")
            _w("")

    # ===================================================================
    # Section 2: FLAKY TESTS (stabilize)
    # ===================================================================
    if flaky_tests:
        _w("## Flaky Tests — Stabilize")
        _w("")
        _w("These tests fail **20-80% of the time** — intermittent failures "
           "that waste CI retries. Fix root cause or add retry/isolation.")
        _w("")

        _w("| # | Test | File | Fail Rate | Builds | Error |")
        _w("|---|------|------|-----------|--------|-------|")
        for i, (tname, fail_b, total_b) in enumerate(flaky_tests[:15], 1):
            rate = fail_b / total_b if total_b else 0
            test_file = _test_name_to_file(tname)
            short_name = tname.rsplit("/", 1)[-1] if "/" in tname else tname
            raw_msg = test_sample_msg.get(tname) or ""
            if _is_wrapper_message(raw_msg):
                msg = "(wrapper — check logs)"
            else:
                msg = format_for_table(raw_msg).replace("|", "/")
            _w(f"| {i} | `{short_name}` | `{test_file}` | {rate:.0%} "
               f"| {fail_b}/{total_b} | `{msg}` |")
        _w("")

        if links:
            for tname, fail_b, total_b in flaky_tests[:5]:
                short = tname.rsplit("/", 1)[-1]
                parts = [f"`{short}`"]
                bid = test_sample_bid.get(tname)
                if bid:
                    tests_url = links.ci_obs_tests(bid)
                    if tests_url:
                        parts.append(f"[test results]({tests_url})")
                    binfo = build_map.get(bid)
                    if binfo:
                        gcs_url = links.gcs_artifacts(
                            binfo["pr_number"], binfo["job_name"], bid)
                        parts.append(f"[GCS artifacts]({gcs_url})")
                affected_prs = sorted(test_pr_sets.get(tname, set()), reverse=True)[:3]
                if affected_prs:
                    pr_refs = ", ".join(f"[#{n}]({links.github_pr(n)})" for n in affected_prs)
                    parts.append(f"PRs: {pr_refs}")
                _w(f"- {' · '.join(parts)}")
            _w("")

    # ===================================================================
    # Section 3: LOW-FREQUENCY TEST FAILURES
    # ===================================================================
    _broken_names = {t[0] for t in broken_tests}
    _flaky_names = {t[0] for t in flaky_tests}
    low_freq_tests = [
        (tname, len(test_build_sets[tname]), len(all_test_by_name.get(tname, set())))
        for tname in test_fail_counter
        if tname not in _broken_names
        and tname not in _flaky_names
        and len(test_build_sets[tname]) >= 2
    ]
    if low_freq_tests:
        _w("## Other Test Failures")
        _w("")
        _w("Tests with fewer than 3 failures or <20% fail rate. Monitor these.")
        _w("")
        for tname, fail_b, total_b in sorted(low_freq_tests, key=lambda x: x[1], reverse=True)[:10]:
            rate = fail_b / total_b if total_b else 0
            test_file = _test_name_to_file(tname)
            short = tname.rsplit("/", 1)[-1] if "/" in tname else tname
            pr_hint = ""
            if links:
                example_prs = sorted(test_pr_sets.get(tname, set()), reverse=True)[:2]
                if example_prs:
                    pr_hint = " — " + ", ".join(
                        f"[#{n}]({links.github_pr(n)})" for n in example_prs)
            _w(f"- `{short}` (`{test_file}`) — {fail_b}/{total_b} ({rate:.0%}){pr_hint}")
        _w("")

    if not broken_tests and not flaky_tests and not period_test_failures:
        _w("## Test Failures")
        _w("")
        _w("No individual test failure data available. Run `collect` with "
           "the CI Observability stack running to populate test results.")
        _w("")

    # ===================================================================
    # Section 4: INFRASTRUCTURE SUMMARY (collapsed)
    # ===================================================================
    infra_steps = [(step, cnt) for step, cnt in step_counter.most_common()
                   if step_infra.get(step)]
    code_steps = [(step, cnt) for step, cnt in step_counter.most_common()
                  if not step_infra.get(step)]

    if infra_steps:
        _w("## Infrastructure Failures")
        _w("")
        _w(f"**{infra_step_builds} infrastructure step failures** consuming "
           f"~{infra_wasted:.0f}h of CI time. These are **not code bugs** — "
           "retesting works around them.")
        _w("")
        _w("| Step | Failures | Status |")
        _w("|------|----------|--------|")

        period_msgs = [m for m in all_fail_msgs if m["build_id"] in failed_build_ids]
        # Group error messages by infra step
        step_msgs: dict[str, Counter[str]] = defaultdict(Counter)
        step_msg_raw: dict[str, dict[str, str]] = defaultdict(dict)
        for bid in infra_bids:
            for s in step_by_build.get(bid, []):
                if s.get("is_infra") and s.get("level") == "Error":
                    for m in period_msgs:
                        if m["build_id"] == bid:
                            norm = _normalize_message(m["message"])
                            step_msgs[s["step_name"]][norm] += m.get("count", 1)
                            if norm not in step_msg_raw[s["step_name"]]:
                                step_msg_raw[s["step_name"]][norm] = m["message"][:100]

        for step, cnt in infra_steps:
            top_msg = ""
            if step_msgs.get(step):
                top_norm, _ = step_msgs[step].most_common(1)[0]
                top_msg = step_msg_raw.get(step, {}).get(top_norm, "")[:60]
            _w(f"| `{step}` | {cnt} | {top_msg} |")
        _w("")

        _w("**Action:** Report to the CI platform team. Key patterns:")
        _w("")
        for step, cnt in infra_steps[:3]:
            if step_msgs.get(step):
                top_norm, top_cnt = step_msgs[step].most_common(1)[0]
                raw = step_msg_raw.get(step, {}).get(top_norm, top_norm)
                _w(f"- **`{step}`** ({cnt} failures): `{raw[:120]}`")
        _w("")

    # ===================================================================
    # Section 5: MANIFEST REGRESSION CHECK (keep)
    # ===================================================================
    manifest_prs = [p for p in period_prs if _is_manifest_update_pr(p)]

    build_start_map: dict[str, str] = {
        b["build_id"]: b.get("started_at") or "" for b in period_builds
    }
    manifest_regressions = _detect_manifest_regressions(
        manifest_prs, period_builds, all_steps, build_start_map,
    )

    if manifest_regressions:
        code_regressions = [r for r in manifest_regressions if not r["is_infra"]]

        if code_regressions:
            _w("## Manifest-Induced Regressions")
            _w("")
            _w("Test steps whose failure rate spiked after a manifest/image update:")
            _w("")
            _w("| Step | Before | After | Δ | Manifest PR |")
            _w("|------|--------|-------|---|-------------|")
            for r in code_regressions:
                mpr = r["manifest_pr"]
                pr_ref = f"#{mpr['number']}"
                if links:
                    pr_ref = f"[#{mpr['number']}]({links.github_pr(mpr['number'])})"
                _w(f"| `{r['step']}` | {r['before_rate']:.0%} | {r['after_rate']:.0%} "
                   f"| +{r['increase']:.0%} | {pr_ref} |")
            _w("")
            _w("**Action:** Compare old/new image SHAs in `get_all_manifests.sh` "
               "or `build/operands-map.yaml`. Check the upstream changelog.")
            _w("")

    elif manifest_prs:
        _w("## Manifest-Induced Regressions")
        _w("")
        _w(f"Checked {len(manifest_prs)} manifest-update PRs — no test "
           "failure-rate spikes detected. Image bumps are not the cause.")
        _w("")

    # ===================================================================
    # Section 6: RECOMMENDATIONS (concise, prioritized)
    # ===================================================================
    _w("## Recommended Actions")
    _w("")

    rec_num = 1
    for tname, fail_b, total_b in broken_tests[:5]:
        rate = fail_b / total_b if total_b else 0
        test_file = _test_name_to_file(tname)
        short = tname.rsplit("/", 1)[-1] if "/" in tname else tname
        raw_msg = test_sample_msg.get(tname) or ""
        if _is_wrapper_message(raw_msg):
            msg = "(wrapper — check logs for real error)"
        else:
            msg = format_for_table(raw_msg, max_chars=100)

        # Compute regression onset for the recommendation
        onset = _detect_regression_onset(
            tname, all_test_results, build_map, all_prs, links,
        )
        cause_hint = ""
        if onset and onset.get("causal_pr"):
            pr_num = onset["causal_pr"]
            pr_ref = f"#{pr_num}"
            if links:
                pr_ref = f"[#{pr_num}]({links.github_pr(pr_num)})"
            if onset["pattern"] == "pr_under_test":
                cause_hint = f" **Start with {pr_ref}** (this PR causes the failure)."
            else:
                cause_hint = (f" **Start with {pr_ref}** "
                              f"(merged to main before failures began).")

        _w(f"{rec_num}. **Fix `{short}`** in `{test_file}` — "
           f"fails {rate:.0%}, {fail_b} builds. `{msg}`{cause_hint}")
        rec_num += 1

    for tname, fail_b, total_b in flaky_tests[:3]:
        rate = fail_b / total_b if total_b else 0
        test_file = _test_name_to_file(tname)
        short = tname.rsplit("/", 1)[-1] if "/" in tname else tname
        pr_hint = ""
        if links:
            example_prs = sorted(test_pr_sets.get(tname, set()), reverse=True)[:2]
            if example_prs:
                pr_hint = " — see " + ", ".join(
                    f"[#{n}]({links.github_pr(n)})" for n in example_prs)
        _w(f"{rec_num}. **Stabilize `{short}`** in `{test_file}` — "
           f"flaky at {rate:.0%}, {fail_b}/{total_b} builds{pr_hint}")
        rec_num += 1

    if infra_steps:
        top_infra = infra_steps[0]
        _w(f"{rec_num}. **Report infra:** `{top_infra[0]}` — "
           f"{top_infra[1]} failures, ~{infra_wasted:.0f}h wasted. "
           f"Not a code bug — report to CI platform team.")
        rec_num += 1

    if not broken_tests and not flaky_tests and not infra_steps:
        _w("No actionable failures detected in this period.")

    _w("")

    # ===================================================================
    # Footer: links + stats
    # ===================================================================
    if links:
        _w(local_access_appendix(links))
        _w("")

    _w("---")
    _w(f"*Analyzed {len(period_builds)} builds across {len(period_prs)} PRs, "
       f"{len(test_fail_counter)} distinct test failures, "
       f"{sum(step_counter.values())} step errors.*")
    _w("")

    return "\n".join(lines)
