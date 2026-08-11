"""AI adoption metrics: track labeled AI-assisted commits over time."""

from __future__ import annotations

from collections import defaultdict

from store.db import Store


def _month(iso: str | None) -> str | None:
    if not iso:
        return None
    return iso[:7]


def compute(store: Store) -> dict:
    """Compute AI adoption metrics, deduplicating by SHA across repos."""
    all_commits = store.get_ai_commits()
    all_prs = store.get_merged_prs(base_branch="main")

    # Deduplicate: same SHA appearing in both upstream and downstream
    seen_shas: dict[str, dict] = {}
    for c in all_commits:
        key = c["sha"]
        if key not in seen_shas:
            seen_shas[key] = c

    unique_commits = list(seen_shas.values())

    # Monthly totals (AI commits)
    ai_by_month: dict[str, int] = defaultdict(int)
    for c in unique_commits:
        m = _month(c.get("date"))
        if m:
            ai_by_month[m] += 1

    # Monthly totals (all PRs, for percentage)
    prs_by_month: dict[str, int] = defaultdict(int)
    for pr in all_prs:
        m = _month(pr.get("merged_at"))
        if m:
            prs_by_month[m] += 1

    # By tool (deduplicated -- one commit can have multiple tools)
    by_tool: dict[str, int] = defaultdict(int)
    sha_tools: dict[str, set[str]] = defaultdict(set)
    for c in all_commits:
        sha_tools[c["sha"]].add(c["tool"])
    for sha, tools in sha_tools.items():
        for t in tools:
            by_tool[t] += 1

    # Monthly by tool
    tool_by_month: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for c in unique_commits:
        m = _month(c.get("date"))
        if m:
            for t in sha_tools.get(c["sha"], set()):
                tool_by_month[m][t] += 1

    all_months = sorted(set(ai_by_month) | set(prs_by_month))

    months = []
    for m in all_months:
        ai_count = ai_by_month.get(m, 0)
        pr_count = prs_by_month.get(m, 0)
        pct = round(ai_count / pr_count * 100, 1) if pr_count > 0 else 0
        months.append({
            "month": m,
            "ai_commits": ai_count,
            "total_prs": pr_count,
            "ai_pct": pct,
            "by_tool": dict(tool_by_month.get(m, {})),
        })

    tools = [{"tool": t, "count": c} for t, c in sorted(by_tool.items(), key=lambda x: -x[1])]

    return {
        "total_ai_commits": len(unique_commits),
        "total_commits": len(all_prs),
        "overall_pct": round(len(unique_commits) / len(all_prs) * 100, 1) if all_prs else 0,
        "by_tool": tools,
        "months": months,
    }
