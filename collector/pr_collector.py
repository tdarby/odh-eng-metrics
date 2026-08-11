"""Collect merged PRs from git log on the bare clone -- zero API calls.

For squash merges, GitHub preserves the author date as the date of the first
commit on the branch, while the committer date is the merge time.  We exploit
this to estimate PR cycle time without any API calls.
"""

from __future__ import annotations

import logging
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from store.db import Store

log = logging.getLogger(__name__)

JIRA_RE = re.compile(r"RHOAIENG-\d+")

# GitHub merge commits look like:
#   "Merge pull request #1234 from user/branch"  (merge-commit strategy)
#   "Some title (#1234)"                          (squash-merge strategy)
PR_NUM_RE = re.compile(r"#(\d+)")

AI_PATTERNS: list[re.Pattern] = [
    re.compile(r"Co-Authored-By:.*(Claude|Copilot|GPT|OpenAI)", re.IGNORECASE),
    re.compile(r"Assisted-By:.*(Claude|Copilot|Cursor)", re.IGNORECASE),
    re.compile(r"Generated.{0,5}(with|by).*(Claude|Cursor|Copilot)", re.IGNORECASE),
    re.compile(r"Made-with:\s*Cursor", re.IGNORECASE),
]

COMPONENT_PREFIX_MAP: list[tuple[str, str]] = [
    ("internal/controller/components/", None),  # dynamic: extract dir name
    ("api/components/v1alpha1/", None),          # dynamic: extract from filename
    ("tests/e2e/", None),                        # dynamic: extract from filename
    ("api/datasciencecluster/", "datasciencecluster"),
    ("api/dscinitialization/", "dscinitialization"),
    ("api/services/", "services"),
    ("api/infrastructure/", "infrastructure"),
    ("internal/controller/datasciencecluster/", "datasciencecluster"),
    ("internal/controller/dscinitialization/", "dscinitialization"),
    ("internal/controller/services/", "services"),
    ("config/crd/", "crd-config"),
    ("pkg/", "core-framework"),
    ("cmd/", "cmd"),
    ("Dockerfiles/", "build"),
    ("hack/", "build"),
]

KNOWN_COMPONENTS = {
    "dashboard", "datasciencepipelines", "feastoperator", "kserve", "kueue",
    "llamastackoperator", "mlflowoperator", "modelcontroller", "modelregistry",
    "modelsasservice", "ray", "sparkoperator", "trainer", "trainingoperator",
    "trustyai", "workbenches",
}


def _file_to_component(filepath: str) -> str | None:
    """Map a changed file path to a component name."""
    if filepath.startswith("internal/controller/components/"):
        rest = filepath[len("internal/controller/components/"):]
        comp = rest.split("/")[0] if "/" in rest else None
        return comp if comp in KNOWN_COMPONENTS else None

    if filepath.startswith("api/components/v1alpha1/"):
        fname = filepath[len("api/components/v1alpha1/"):]
        comp = fname.replace("_types.go", "").replace("_webhook.go", "").split("_")[0]
        return comp if comp in KNOWN_COMPONENTS else None

    if filepath.startswith("tests/e2e/") and filepath.endswith("_test.go"):
        fname = filepath[len("tests/e2e/"):]
        comp = fname.replace("_test.go", "")
        return comp if comp in KNOWN_COMPONENTS else None

    for prefix, comp in COMPONENT_PREFIX_MAP:
        if comp and filepath.startswith(prefix):
            return comp

    return None


MANIFEST_UPDATE_PATTERNS = [
    re.compile(r"manifest.{0,10}(sha|commit|hash)", re.IGNORECASE),
    re.compile(r"update.{0,10}manifest", re.IGNORECASE),
    re.compile(r"bump.{0,10}manifest", re.IGNORECASE),
    re.compile(r"chore.*manifest.*SHA", re.IGNORECASE),
]

MANIFEST_FILES = {"get_all_manifests.sh", "build/manifests-config.yaml"}


def _is_manifest_update(title: str, changed_files: list[str]) -> bool:
    """Detect if a PR is a manifest SHA bump (upstream component version change)."""
    if any(p.search(title) for p in MANIFEST_UPDATE_PATTERNS):
        return True
    return any(f in MANIFEST_FILES for f in changed_files)


def _detect_ai(text: str) -> bool:
    """Check if commit text contains AI-assisted markers."""
    return any(p.search(text) for p in AI_PATTERNS)


def _git(repo_path: Path, *args: str, timeout: int = 120) -> str:
    result = subprocess.run(
        ["git", f"--git-dir={repo_path}", *args],
        capture_output=True, text=True, timeout=timeout,
    )
    return result.stdout.strip()


def _resolve_ref(repo_path: Path, branch: str) -> str | None:
    for candidate in [branch, f"origin/{branch}", f"refs/heads/{branch}"]:
        r = subprocess.run(
            ["git", f"--git-dir={repo_path}", "rev-parse", "--verify", candidate],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            return candidate
    return None


COMMIT_MARKER = "COMMIT:"


def collect_prs_from_git(
    store: Store,
    repo_path: Path,
    repo_name: str,
    branch: str = "main",
    lookback_days: int = 365,
) -> int:
    """Parse merge/squash commits on a branch to extract PR data. Zero API calls."""
    ref = _resolve_ref(repo_path, branch)
    if not ref:
        log.warning("Branch %s not found in %s", branch, repo_path)
        return 0

    since = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

    # Pass 1: headers + changed files with line stats.
    # --numstat appends "additions\tdeletions\tfilename" after each commit.
    # Lines starting with COMMIT: are headers; blank lines are separators;
    # everything else is a numstat line belonging to the preceding commit.
    log_output = _git(
        repo_path, "log", ref,
        f"--since={since}",
        f"--format={COMMIT_MARKER}%H|%aI|%cI|%ae|%s",
        "--numstat",
        timeout=300,
    )
    if not log_output:
        return 0

    # Pass 2: bodies for AI detection and extra Jira keys.
    # Use NUL byte as commit separator since body text can contain newlines.
    body_output = _git(
        repo_path, "log", ref,
        f"--since={since}",
        "--format=%H%x00%b%x00",
        timeout=300,
    )
    body_map: dict[str, str] = {}
    if body_output:
        parts = body_output.split("\x00")
        i = 0
        while i < len(parts) - 1:
            sha_part = parts[i].strip()
            body_part = parts[i + 1] if i + 1 < len(parts) else ""
            if sha_part:
                body_map[sha_part] = body_part
            i += 2

    # Parse the header + numstat output into per-commit records.
    commits: list[dict] = []
    current: dict | None = None

    for line in log_output.split("\n"):
        if line.startswith(COMMIT_MARKER):
            if current:
                commits.append(current)
            header = line[len(COMMIT_MARKER):]
            current = {"header": header, "files": [], "additions": 0, "deletions": 0}
        elif current is not None and line.strip():
            # --numstat format: "adds\tdels\tfilename" (binary files use "-")
            parts = line.split("\t", 2)
            if len(parts) == 3:
                adds_str, dels_str, filepath = parts
                current["files"].append(filepath.strip())
                try:
                    current["additions"] += int(adds_str)
                except ValueError:
                    pass  # binary file: "-"
                try:
                    current["deletions"] += int(dels_str)
                except ValueError:
                    pass
            else:
                current["files"].append(line.strip())

    if current:
        commits.append(current)

    count = 0
    parsed = 0
    for entry in commits:
        header_fields = entry["header"].split("|", 4)
        if len(header_fields) < 5:
            continue
        sha, author_date, commit_date, author_email, subject = header_fields
        parsed += 1

        pr_match = PR_NUM_RE.search(subject)
        if not pr_match:
            continue
        pr_number = int(pr_match.group(1))

        body = body_map.get(sha, "")
        jira_keys = sorted(set(JIRA_RE.findall(subject + "\n" + body)))

        full_text = subject + "\n" + body
        is_ai = _detect_ai(full_text)

        changed_files = entry["files"]
        components = sorted({c for f in changed_files if (c := _file_to_component(f))})
        is_manifest = _is_manifest_update(subject, changed_files)

        # Parse additions/deletions from --numstat output if available
        additions = entry.get("additions", 0)
        deletions = entry.get("deletions", 0)

        first_commit_at = _get_first_commit_date(repo_path, sha, author_date, commit_date)

        store.upsert_pr(repo_name, {
            "number": pr_number,
            "title": subject,
            "author": author_email.split("@")[0],
            "created_at": first_commit_at or author_date,
            "merged_at": commit_date,
            "first_commit_at": first_commit_at,
            "base_branch": branch,
            "additions": additions,
            "deletions": deletions,
            "jira_keys": jira_keys,
            "merge_sha": sha,
            "is_ai_assisted": is_ai,
            "changed_files": changed_files,
            "changed_components": components,
            "is_manifest_update": is_manifest,
        })
        count += 1

    log.info("Parsed %d commits, collected %d PRs from git log on %s", parsed, count, branch)
    return count


def collect_open_pr_metadata(
    store: Store,
    repo_name: str,
    cfg: dict | None = None,
) -> int:
    """Fetch metadata from GitHub API for PRs in ci_builds that aren't in merged_prs.

    Fills the ci_pr_metadata table so CI failure analysis can show title, author,
    and JIRA keys for PRs that are still open or were closed without merging.
    """
    import os
    import httpx

    owner, repo = repo_name.split("/", 1)
    token = os.environ.get("GITHUB_TOKEN", "")

    # Find PR numbers in CI builds that have no merged_prs or ci_pr_metadata entry
    rows = store.conn.execute("""
        SELECT DISTINCT cb.pr_number
        FROM ci_builds cb
        LEFT JOIN merged_prs mp
            ON mp.number = cb.pr_number AND mp.repo = ?
        LEFT JOIN ci_pr_metadata cpm
            ON cpm.number = cb.pr_number AND cpm.repo = ?
        WHERE mp.number IS NULL AND cpm.number IS NULL
        ORDER BY cb.pr_number DESC
    """, (repo_name, repo_name)).fetchall()

    if not rows:
        log.info("No unmatched CI PRs need metadata")
        return 0

    pr_numbers = [r["pr_number"] for r in rows]
    log.info("Fetching GitHub metadata for %d open/unmerged PRs...", len(pr_numbers))

    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    count = 0
    with httpx.Client(base_url="https://api.github.com", headers=headers) as client:
        for pr_num in pr_numbers:
            try:
                resp = client.get(f"/repos/{owner}/{repo}/pulls/{pr_num}", timeout=15)
                if resp.status_code == 404:
                    continue
                if resp.status_code == 403:
                    log.warning("GitHub API rate limited; stopping PR metadata fetch")
                    break
                resp.raise_for_status()
                pr_data = resp.json()
            except Exception:
                log.debug("Failed to fetch PR #%d from GitHub", pr_num, exc_info=True)
                continue

            title = pr_data.get("title", "")
            author = pr_data.get("user", {}).get("login", "")
            state = pr_data.get("state", "")
            body = pr_data.get("body") or ""
            jira_keys = sorted(set(JIRA_RE.findall(title + "\n" + body)))

            # Fetch changed files (paginated, but we only need first page for components)
            changed_files: list[str] = []
            changed_components: list[str] = []
            try:
                files_resp = client.get(
                    f"/repos/{owner}/{repo}/pulls/{pr_num}/files",
                    params={"per_page": 100},
                    timeout=15,
                )
                if files_resp.status_code == 200:
                    changed_files = [f["filename"] for f in files_resp.json()]
                    changed_components = sorted(
                        {c for f in changed_files if (c := _file_to_component(f))}
                    )
            except Exception:
                pass

            store.upsert_ci_pr_metadata(
                repo=repo_name,
                number=pr_num,
                title=title,
                author=author,
                state=state,
                jira_keys=jira_keys,
                changed_files=changed_files,
                changed_components=changed_components,
            )
            count += 1

    log.info("Stored metadata for %d open/unmerged PRs", count)
    return count


def _get_first_commit_date(
    repo_path: Path, sha: str, author_date: str, commit_date: str,
) -> str | None:
    """Estimate when work on a PR started.

    For squash merges (1 parent): GitHub preserves the author date as the
    date of the first commit on the topic branch. If author_date != commit_date
    that gives us the cycle time.

    For merge commits (2+ parents): walk from the merge-base to the branch tip
    and return the author date of the earliest commit.
    """
    parents = _git(repo_path, "rev-list", "--parents", "-1", sha)
    parent_shas = parents.split()[1:]

    if len(parent_shas) < 2:
        # Squash merge.  If author date differs from committer date, the
        # author date approximates the first commit on the topic branch.
        if author_date != commit_date:
            return author_date
        # Dates are identical -- can't distinguish, return author_date anyway
        # (will show as ~0h cycle time, which is filtered in lead_time.py)
        return author_date

    # True merge commit: walk the topic branch to find the earliest commit
    first_parent, branch_tip = parent_shas[0], parent_shas[1]
    merge_base = _git(repo_path, "merge-base", first_parent, branch_tip)
    if not merge_base:
        return author_date

    earliest = _git(
        repo_path, "log", "--reverse", "--format=%aI",
        f"{merge_base}..{branch_tip}", "--",
    )
    if earliest:
        return earliest.split("\n")[0].strip()

    return author_date
