"""Analyze code risk using the hotspots CLI tool or gocyclo fallback.

Runs static analysis + git churn analysis to identify high-risk functions,
then maps each function to an ODH operator component via directory structure.
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from collector.pr_collector import _file_to_component
from store.db import Store

log = logging.getLogger(__name__)


def _run_hotspots(repo_path: Path) -> list[dict] | None:
    """Run 'hotspots analyze --format json' and parse output."""
    try:
        result = subprocess.run(
            ["hotspots", "analyze", "--format", "json", str(repo_path)],
            capture_output=True, text=True, timeout=300,
        )
        # hotspots may exit non-zero even with valid JSON output
        if not result.stdout.strip():
            log.debug("hotspots produced no output (exit %d): %s", result.returncode, result.stderr[:200])
            return None
        return json.loads(result.stdout)
    except FileNotFoundError:
        log.info("hotspots CLI not found, will try gocyclo fallback")
        return None
    except (json.JSONDecodeError, subprocess.TimeoutExpired) as e:
        log.warning("hotspots failed: %s", e)
        return None


def _run_gocyclo(repo_path: Path) -> list[dict] | None:
    """Fallback: run gocyclo for complexity-only analysis (no churn)."""
    try:
        result = subprocess.run(
            ["gocyclo", "-json", str(repo_path)],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            log.debug("gocyclo exited with code %d", result.returncode)
            return None

        entries = []
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                entries.append(entry)
            except json.JSONDecodeError:
                continue
        return entries if entries else None
    except FileNotFoundError:
        log.info("gocyclo not found either; skipping code analysis")
        return None
    except subprocess.TimeoutExpired:
        log.warning("gocyclo timed out")
        return None


def _risk_band(score: float) -> str:
    if score >= 9.0:
        return "Critical"
    if score >= 6.0:
        return "High"
    if score >= 3.0:
        return "Medium"
    return "Low"


def _find_worktree(bare_path: Path) -> Path | None:
    """Find a non-bare working tree for a bare repo.

    hotspots needs actual source files, but our repos are bare clones.
    The bare repo is named <reponame>.git by repo_manager, so look for
    ~/git/<reponame> as the most reliable match.
    """
    repo_name = bare_path.name.removesuffix(".git")
    candidate = Path.home() / "git" / repo_name
    if candidate.is_dir() and (candidate / ".git").is_dir():
        log.info("Found working tree at %s", candidate)
        return candidate
    return None


def analyze_code_risk(
    store: Store,
    repo_path: Path,
    repo_name: str,
    force: bool = False,
) -> int:
    """Run code risk analysis and store results.

    Skips if scores already exist for this repo (unless force=True).
    """
    if not force:
        existing = store.get_code_risk_scores(repo=repo_name)
        if existing:
            log.info("Code risk scores already collected (%d functions) — skipping", len(existing))
            return 0

    now = datetime.now(timezone.utc).isoformat()
    count = 0

    analysis_path = repo_path
    if not (repo_path / "go.mod").exists() and not (repo_path / "main.go").exists():
        worktree = _find_worktree(repo_path)
        if worktree:
            analysis_path = worktree
        else:
            log.warning("Repo at %s appears to be bare and no working tree found; "
                        "hotspots needs source files", repo_path)

    # hotspots emits absolute file paths; we need to strip the repo prefix
    # so _file_to_component can match on relative paths.
    strip_prefix = str(analysis_path).rstrip("/") + "/"

    hotspots_data = _run_hotspots(analysis_path)
    if hotspots_data is not None:
        functions = hotspots_data if isinstance(hotspots_data, list) else hotspots_data.get("functions", [])
        for fn in functions:
            filepath = fn.get("file", "")
            func_name = fn.get("function", fn.get("name", ""))
            # hotspots uses "lrs" (Logarithmic Risk Score) and nested "metrics"
            risk_score = fn.get("lrs", fn.get("risk_score", fn.get("score", 0.0)))
            metrics = fn.get("metrics", {})
            complexity = float(metrics.get("cc", fn.get("complexity", 0)))
            churn = metrics.get("touches", fn.get("churn", fn.get("churn_30d", 0)))
            band = fn.get("band", _risk_band(risk_score))

            # Strip absolute path prefix and any leading "./" for component mapping.
            rel_path = filepath
            if rel_path.startswith(strip_prefix):
                rel_path = rel_path[len(strip_prefix):]
            rel_path = rel_path.lstrip("./")

            component = _file_to_component(rel_path)
            store.upsert_code_risk(
                repo=repo_name,
                file=rel_path,
                function=func_name,
                component=component,
                complexity=complexity,
                churn_30d=churn,
                risk_score=round(risk_score, 2),
                risk_band=band.capitalize() if band else _risk_band(risk_score),
                analyzed_at=now,
            )
            count += 1

        log.info("Stored %d function risk scores from hotspots", count)
        return count

    gocyclo_data = _run_gocyclo(repo_path)
    if gocyclo_data is not None:
        for entry in gocyclo_data:
            filepath = entry.get("file", entry.get("pos", {}).get("filename", ""))
            func_name = entry.get("function", entry.get("func_name", ""))
            complexity = float(entry.get("complexity", 0))
            # gocyclo has no churn data; use complexity as a rough risk proxy
            risk_score = min(complexity / 3.0, 10.0)

            component = _file_to_component(filepath)
            store.upsert_code_risk(
                repo=repo_name,
                file=filepath,
                function=func_name,
                component=component,
                complexity=complexity,
                churn_30d=None,
                risk_score=round(risk_score, 1),
                risk_band=_risk_band(risk_score),
                analyzed_at=now,
            )
            count += 1

        log.info("Stored %d function complexity scores from gocyclo (no churn data)", count)
        return count

    log.info("No code analysis tool available; skipping code risk scoring")
    return 0
