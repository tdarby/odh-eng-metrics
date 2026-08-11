"""Collect AI Bug Automation Readiness assessments for repos mapped to JIRA projects.

Uses https://github.com/ugiordan/ai-bug-automation-readiness — a zero-dependency
Python tool that evaluates repos across 20 checks in 4 phases (Understand, Navigate,
Verify, Submit).  The tool repo is auto-cloned on first run.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from store.db import Store

log = logging.getLogger(__name__)

TOOL_REPO_URL = "https://github.com/ugiordan/ai-bug-automation-readiness.git"
TOOL_DIR_NAME = "ai-bug-automation-readiness"

READINESS_LEVELS = {
    range(80, 101): "Ready",
    range(60, 80): "Partially Ready",
    range(40, 60): "Needs Work",
    range(0, 40): "Not Ready",
}


def _readiness_level(score: float) -> str:
    s = int(round(score))
    for rng, label in READINESS_LEVELS.items():
        if s in rng:
            return label
    return "Unknown"


def _repo_name(url: str) -> str:
    """Extract a short name from a repo URL (e.g. 'opendatahub-operator')."""
    path = urlparse(url).path.rstrip("/").rstrip(".git")
    return path.rsplit("/", 1)[-1]


def _ensure_tool(data_dir: Path) -> Path | None:
    """Clone or update the readiness tool repo.  Returns path to assess.py."""
    tool_dir = data_dir / TOOL_DIR_NAME
    assess_py = tool_dir / "assess.py"

    if assess_py.exists():
        log.info("Readiness tool already cloned at %s", tool_dir)
        try:
            subprocess.run(
                ["git", "-C", str(tool_dir), "pull", "--ff-only"],
                capture_output=True, timeout=60,
            )
        except Exception:
            pass
        return assess_py

    log.info("Cloning readiness tool from %s", TOOL_REPO_URL)
    try:
        subprocess.run(
            ["git", "clone", "--depth=1", TOOL_REPO_URL, str(tool_dir)],
            capture_output=True, timeout=120, check=True,
        )
    except subprocess.CalledProcessError as e:
        log.error("Failed to clone readiness tool: %s", e.stderr[:500] if e.stderr else e)
        return None

    if not assess_py.exists():
        log.error("assess.py not found after cloning %s", tool_dir)
        return None
    return assess_py


def _clone_or_update(repo_url: str, cache_dir: Path) -> Path:
    """Shallow-clone a repo (or pull if already cached). Returns local path."""
    name = _repo_name(repo_url)
    dest = cache_dir / name
    if dest.exists() and (dest / ".git").exists():
        log.info("Updating cached clone: %s", dest)
        subprocess.run(["git", "-C", str(dest), "fetch", "--depth=1"],
                       capture_output=True, timeout=120)
        subprocess.run(["git", "-C", str(dest), "reset", "--hard", "FETCH_HEAD"],
                       capture_output=True, timeout=60)
    else:
        dest.mkdir(parents=True, exist_ok=True)
        log.info("Shallow-cloning %s → %s", repo_url, dest)
        subprocess.run(
            ["git", "clone", "--depth=1", repo_url, str(dest)],
            capture_output=True, timeout=300, check=True,
        )
    return dest


def _run_assessment(assess_py: Path, repo_path: Path) -> dict | None:
    """Run `python assess.py <repo> --format json` and return parsed JSON."""
    cmd = [sys.executable, str(assess_py), str(repo_path), "--format", "json"]
    log.info("Running: %s", " ".join(cmd))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        log.error("Readiness assessment timed out for %s", repo_path)
        return None

    if result.returncode not in (0, 1):
        log.warning("assess.py failed (rc=%d): %s", result.returncode,
                     result.stderr[:500] if result.stderr else "")
        return None

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        log.error("Failed to parse JSON from assess.py output for %s", repo_path)
        if result.stdout:
            log.debug("stdout (first 500): %s", result.stdout[:500])
        return None


def _extract_findings(data: dict) -> list[dict]:
    """Normalize per-check results into the findings format the DB/report expects.

    The tool outputs checks under various possible keys.  We normalize to:
    [{"attribute": {"id": "<check_id>"}, "score": <0-100>, "name": "...", "category": "..."}]
    """
    findings = []
    checks = data.get("checks") or data.get("details") or data.get("results") or []

    if isinstance(checks, list):
        for c in checks:
            check_id = c.get("id") or c.get("check") or ""
            findings.append({
                "attribute": {"id": check_id},
                "score": c.get("score", 0),
                "name": c.get("name", check_id.replace("_", " ").title()),
                "category": c.get("category", ""),
                "weight": c.get("weight", 0),
            })
    elif isinstance(checks, dict):
        for check_id, val in checks.items():
            score = val if isinstance(val, (int, float)) else val.get("score", 0) if isinstance(val, dict) else 0
            findings.append({
                "attribute": {"id": check_id},
                "score": score,
                "name": check_id.replace("_", " ").title(),
            })

    return findings


def collect_assessments(store: Store, cfg: dict, collection_name: str = "ai-bug-bash",
                        force: bool = False) -> int:
    """Run AI Bug Automation Readiness on repos configured in a JIRA collection's project_repos.

    Returns the number of assessments stored.
    """
    collections = cfg.get("jira", {}).get("collections", [])
    coll = next((c for c in collections if c["name"] == collection_name), None)
    if not coll:
        log.warning("No collection '%s' found in config", collection_name)
        return 0

    project_repos = coll.get("project_repos", {})
    if not project_repos:
        log.info("No project_repos configured for '%s' — skipping readiness", collection_name)
        return 0

    data_dir = Path(cfg["collection"]["data_dir"])
    assess_py = _ensure_tool(data_dir)
    if not assess_py:
        return 0

    clone_cache = data_dir / "agentready-repos"
    stored = 0
    if force:
        existing = set()
    else:
        existing = {(r["repo_url"], r["project"]) for r in store.get_agentready_assessments()}

    for project, repos in sorted(project_repos.items()):
        if not repos:
            continue
        for repo_url in repos:
            name = _repo_name(repo_url)
            if (repo_url, project) in existing:
                log.info("Skipping %s (%s) — already assessed", name, project)
                continue
            log.info("Assessing %s (%s)...", name, project)

            try:
                repo_path = _clone_or_update(repo_url, clone_cache)
            except subprocess.CalledProcessError as e:
                log.error("Failed to clone %s: %s", repo_url, e)
                continue

            assessment = _run_assessment(assess_py, repo_path)
            if not assessment:
                continue

            overall = assessment.get("overall_score", 0)
            cert = assessment.get("readiness_level") or assessment.get("level") or _readiness_level(overall)
            findings = _extract_findings(assessment)

            store.upsert_agentready_assessment(
                repo_url=repo_url,
                project=project,
                overall_score=overall,
                certification_level=cert,
                attributes_assessed=sum(1 for f in findings if f.get("score", 0) > 0),
                attributes_total=len(findings),
                findings_json=json.dumps(findings),
                assessed_at=datetime.utcnow().isoformat(),
            )
            stored += 1
            log.info("  %s: score=%.1f, level=%s (%d checks)",
                     name, overall, cert, len(findings))

    return stored
