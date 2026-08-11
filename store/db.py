"""SQLite storage for collected events and computed metrics."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS releases (
    tag         TEXT PRIMARY KEY,
    published   TEXT NOT NULL,
    prerelease  INTEGER NOT NULL DEFAULT 0,
    is_patch    INTEGER NOT NULL DEFAULT 0,
    is_ea       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS merged_prs (
    repo        TEXT NOT NULL,
    number      INTEGER NOT NULL,
    title       TEXT,
    author      TEXT,
    created_at  TEXT,
    merged_at   TEXT NOT NULL,
    first_commit_at TEXT,
    base_branch TEXT,
    additions   INTEGER DEFAULT 0,
    deletions   INTEGER DEFAULT 0,
    jira_keys   TEXT,
    PRIMARY KEY (repo, number)
);

CREATE TABLE IF NOT EXISTS reverts (
    repo        TEXT NOT NULL,
    sha         TEXT PRIMARY KEY,
    date        TEXT NOT NULL,
    reverted_sha TEXT,
    message     TEXT
);

CREATE TABLE IF NOT EXISTS cherry_picks (
    repo        TEXT NOT NULL,
    pr_number   INTEGER NOT NULL,
    target_branch TEXT NOT NULL,
    title       TEXT,
    author      TEXT,
    merged_at   TEXT,
    PRIMARY KEY (repo, pr_number)
);

CREATE TABLE IF NOT EXISTS downstream_branches (
    name        TEXT PRIMARY KEY,
    first_commit_date TEXT,
    is_ea       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS branch_arrivals (
    pr_repo     TEXT NOT NULL,
    pr_number   INTEGER NOT NULL,
    branch      TEXT NOT NULL,
    arrived_at  TEXT,
    PRIMARY KEY (pr_repo, pr_number, branch)
);

CREATE TABLE IF NOT EXISTS ai_assisted_commits (
    repo        TEXT NOT NULL,
    sha         TEXT NOT NULL,
    date        TEXT NOT NULL,
    message     TEXT,
    tool        TEXT NOT NULL,
    PRIMARY KEY (repo, sha, tool)
);

CREATE TABLE IF NOT EXISTS ci_builds (
    build_id    TEXT PRIMARY KEY,
    pr_number   INTEGER NOT NULL,
    job_name    TEXT NOT NULL,
    duration_seconds REAL,
    result      TEXT NOT NULL DEFAULT 'unknown',
    started_at  TEXT,
    base_sha    TEXT,
    pull_sha    TEXT
);

CREATE TABLE IF NOT EXISTS ci_pr_metadata (
    repo        TEXT NOT NULL,
    number      INTEGER NOT NULL,
    title       TEXT,
    author      TEXT,
    state       TEXT,
    jira_keys   TEXT,
    changed_files TEXT,
    changed_components TEXT,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (repo, number)
);

CREATE TABLE IF NOT EXISTS code_risk_scores (
    repo        TEXT NOT NULL,
    file        TEXT NOT NULL,
    function    TEXT NOT NULL,
    component   TEXT,
    complexity  REAL,
    churn_30d   INTEGER,
    risk_score  REAL,
    risk_band   TEXT,
    analyzed_at TEXT NOT NULL,
    PRIMARY KEY (repo, file, function)
);

CREATE TABLE IF NOT EXISTS ci_build_steps (
    build_id    TEXT NOT NULL,
    step_name   TEXT NOT NULL,
    duration_seconds REAL,
    level       TEXT,
    is_infra    INTEGER DEFAULT 0,
    PRIMARY KEY (build_id, step_name)
);

CREATE TABLE IF NOT EXISTS ci_build_failure_messages (
    build_id    TEXT NOT NULL,
    message     TEXT NOT NULL,
    source      TEXT,
    count       INTEGER DEFAULT 1,
    PRIMARY KEY (build_id, message)
);

CREATE TABLE IF NOT EXISTS ci_test_results (
    build_id    TEXT NOT NULL,
    test_name   TEXT NOT NULL,
    suite       TEXT,
    test_variant TEXT,
    status      TEXT NOT NULL,
    duration_seconds REAL,
    is_leaf     INTEGER DEFAULT 1,
    failure_message TEXT,
    PRIMARY KEY (build_id, test_name, test_variant)
);

CREATE TABLE IF NOT EXISTS jira_issues (
    key             TEXT PRIMARY KEY,
    summary         TEXT,
    issue_type      TEXT,
    priority        TEXT,
    status          TEXT,
    status_category TEXT,
    assignee        TEXT,
    components      TEXT,
    labels          TEXT,
    fix_versions    TEXT,
    story_points    REAL,
    created         TEXT,
    resolved        TEXT,
    epic_key        TEXT,
    description     TEXT,
    comments        TEXT,
    fetched_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jira_collection_issues (
    collection_name TEXT NOT NULL,
    issue_key       TEXT NOT NULL,
    PRIMARY KEY (collection_name, issue_key)
);

CREATE TABLE IF NOT EXISTS metrics_cache (
    metric      TEXT NOT NULL,
    window      TEXT NOT NULL,
    value       TEXT NOT NULL,
    computed_at TEXT NOT NULL,
    PRIMARY KEY (metric, window)
);

CREATE TABLE IF NOT EXISTS agentready_assessments (
    repo_url            TEXT NOT NULL,
    project             TEXT NOT NULL,
    overall_score       REAL NOT NULL,
    certification_level TEXT NOT NULL,
    attributes_assessed INTEGER NOT NULL DEFAULT 0,
    attributes_total    INTEGER NOT NULL DEFAULT 0,
    findings_json       TEXT,
    assessed_at         TEXT NOT NULL,
    PRIMARY KEY (repo_url, project)
);

CREATE TABLE IF NOT EXISTS component_manifest_pins (
    component   TEXT NOT NULL,
    repo_url    TEXT NOT NULL,
    branch      TEXT,
    pinned_sha  TEXT NOT NULL,
    source_path TEXT,
    captured_at TEXT NOT NULL,
    pr_number   INTEGER,
    PRIMARY KEY (component, captured_at)
);

CREATE TABLE IF NOT EXISTS manifest_sha_deltas (
    component       TEXT NOT NULL,
    old_sha         TEXT NOT NULL,
    new_sha         TEXT NOT NULL,
    repo_url        TEXT NOT NULL,
    commit_count    INTEGER,
    commits_json    TEXT,
    pr_number       INTEGER,
    fetched_at      TEXT NOT NULL,
    PRIMARY KEY (component, old_sha, new_sha)
);
"""


class Store:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after initial schema."""
        migrations = [
            ("ci_builds", "started_at", "TEXT"),
            ("merged_prs", "merge_sha", "TEXT"),
            ("merged_prs", "is_ai_assisted", "INTEGER DEFAULT 0"),
            ("merged_prs", "changed_files", "TEXT"),
            ("merged_prs", "changed_components", "TEXT"),
            ("reverts", "reverted_pr", "INTEGER"),
            ("ci_builds", "peak_cpu_cores", "REAL"),
            ("ci_builds", "peak_memory_bytes", "REAL"),
            ("ci_builds", "total_step_seconds", "REAL"),
            ("ci_builds", "base_sha", "TEXT"),
            ("ci_builds", "pull_sha", "TEXT"),
            ("jira_issues", "description", "TEXT"),
            ("jira_issues", "comments", "TEXT"),
            ("merged_prs", "is_manifest_update", "INTEGER DEFAULT 0"),
        ]
        for table, column, col_type in migrations:
            try:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
                self.conn.commit()
            except sqlite3.OperationalError:
                pass

    def upsert_release(self, tag: str, published: str, prerelease: bool, is_patch: bool, is_ea: bool) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO releases (tag, published, prerelease, is_patch, is_ea) VALUES (?, ?, ?, ?, ?)",
            (tag, published, int(prerelease), int(is_patch), int(is_ea)),
        )
        self.conn.commit()

    def upsert_pr(self, repo: str, pr: dict) -> None:
        jira_keys = json.dumps(pr.get("jira_keys", []))
        changed_files = json.dumps(pr.get("changed_files", []))
        changed_components = json.dumps(pr.get("changed_components", []))
        self.conn.execute(
            """INSERT OR REPLACE INTO merged_prs
               (repo, number, title, author, created_at, merged_at, first_commit_at,
                base_branch, additions, deletions, jira_keys,
                merge_sha, is_ai_assisted, changed_files, changed_components,
                is_manifest_update)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                repo, pr["number"], pr.get("title"), pr.get("author"),
                pr.get("created_at"), pr["merged_at"], pr.get("first_commit_at"),
                pr.get("base_branch"), pr.get("additions", 0), pr.get("deletions", 0),
                jira_keys, pr.get("merge_sha"), int(pr.get("is_ai_assisted", False)),
                changed_files, changed_components,
                int(pr.get("is_manifest_update", False)),
            ),
        )
        self.conn.commit()

    def upsert_ci_pr_metadata(self, repo: str, number: int, title: str | None,
                               author: str | None, state: str | None,
                               jira_keys: list[str] | None = None,
                               changed_files: list[str] | None = None,
                               changed_components: list[str] | None = None,
                               fetched_at: str | None = None) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO ci_pr_metadata
               (repo, number, title, author, state, jira_keys,
                changed_files, changed_components, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (repo, number, title, author, state,
             json.dumps(jira_keys or []),
             json.dumps(changed_files or []),
             json.dumps(changed_components or []),
             fetched_at or datetime.utcnow().isoformat()),
        )
        self.conn.commit()

    def upsert_revert(self, repo: str, sha: str, date: str, reverted_sha: str | None,
                       message: str, reverted_pr: int | None = None) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO reverts
               (repo, sha, date, reverted_sha, message, reverted_pr)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (repo, sha, date, reverted_sha, message, reverted_pr),
        )
        self.conn.commit()

    def upsert_cherry_pick(self, repo: str, pr_number: int, target_branch: str,
                           title: str, author: str, merged_at: str | None) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO cherry_picks
               (repo, pr_number, target_branch, title, author, merged_at) VALUES (?, ?, ?, ?, ?, ?)""",
            (repo, pr_number, target_branch, title, author, merged_at),
        )
        self.conn.commit()

    def upsert_downstream_branch(self, name: str, first_commit_date: str | None, is_ea: bool) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO downstream_branches (name, first_commit_date, is_ea) VALUES (?, ?, ?)",
            (name, first_commit_date, int(is_ea)),
        )
        self.conn.commit()

    def upsert_branch_arrival(self, pr_repo: str, pr_number: int, branch: str, arrived_at: str | None) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO branch_arrivals (pr_repo, pr_number, branch, arrived_at) VALUES (?, ?, ?, ?)",
            (pr_repo, pr_number, branch, arrived_at),
        )
        self.conn.commit()

    def upsert_ai_commit(self, repo: str, sha: str, date: str, message: str, tool: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO ai_assisted_commits (repo, sha, date, message, tool) VALUES (?, ?, ?, ?, ?)",
            (repo, sha, date, message[:200], tool),
        )
        self.conn.commit()

    def get_ai_commits(self, repo: str | None = None) -> list[dict]:
        q = "SELECT * FROM ai_assisted_commits"
        params: list[Any] = []
        if repo:
            q += " WHERE repo = ?"
            params.append(repo)
        q += " ORDER BY date"
        return [dict(r) for r in self.conn.execute(q, params).fetchall()]

    def upsert_ci_build(self, build_id: str, pr_number: int, job_name: str,
                        duration_seconds: float | None, result: str,
                        started_at: str | None = None,
                        peak_cpu_cores: float | None = None,
                        peak_memory_bytes: float | None = None,
                        total_step_seconds: float | None = None,
                        base_sha: str | None = None,
                        pull_sha: str | None = None) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO ci_builds
               (build_id, pr_number, job_name, duration_seconds, result, started_at,
                peak_cpu_cores, peak_memory_bytes, total_step_seconds,
                base_sha, pull_sha)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (build_id, pr_number, job_name, duration_seconds, result, started_at,
             peak_cpu_cores, peak_memory_bytes, total_step_seconds,
             base_sha, pull_sha),
        )
        self.conn.commit()

    def get_ci_builds(self, pr_number: int | None = None) -> list[dict]:
        q = "SELECT * FROM ci_builds"
        params: list[Any] = []
        if pr_number is not None:
            q += " WHERE pr_number = ?"
            params.append(pr_number)
        q += " ORDER BY build_id"
        return [dict(r) for r in self.conn.execute(q, params).fetchall()]

    def get_ci_build_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) as n FROM ci_builds").fetchone()
        return row["n"] if row else 0

    def upsert_build_step(self, build_id: str, step_name: str,
                          duration_seconds: float | None, level: str | None,
                          is_infra: bool = False) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO ci_build_steps
               (build_id, step_name, duration_seconds, level, is_infra)
               VALUES (?, ?, ?, ?, ?)""",
            (build_id, step_name, duration_seconds, level, int(is_infra)),
        )
        self.conn.commit()

    def upsert_build_failure_message(self, build_id: str, message: str,
                                     source: str | None = None,
                                     count: int = 1) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO ci_build_failure_messages
               (build_id, message, source, count) VALUES (?, ?, ?, ?)""",
            (build_id, message[:500], source, count),
        )
        self.conn.commit()

    def get_build_steps(self, build_id: str | None = None,
                        level: str | None = None) -> list[dict]:
        q = "SELECT * FROM ci_build_steps WHERE 1=1"
        params: list[Any] = []
        if build_id:
            q += " AND build_id = ?"
            params.append(build_id)
        if level:
            q += " AND level = ?"
            params.append(level)
        q += " ORDER BY build_id, step_name"
        return [dict(r) for r in self.conn.execute(q, params).fetchall()]

    def get_build_failure_messages(self, build_id: str | None = None) -> list[dict]:
        q = "SELECT * FROM ci_build_failure_messages WHERE 1=1"
        params: list[Any] = []
        if build_id:
            q += " AND build_id = ?"
            params.append(build_id)
        q += " ORDER BY build_id, count DESC"
        return [dict(r) for r in self.conn.execute(q, params).fetchall()]

    def get_all_build_steps(self) -> list[dict]:
        """Get all build steps, optimized for batch processing."""
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM ci_build_steps ORDER BY build_id, step_name"
        ).fetchall()]

    def get_all_build_failure_messages(self) -> list[dict]:
        """Get all failure messages, optimized for batch processing."""
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM ci_build_failure_messages ORDER BY build_id, count DESC"
        ).fetchall()]

    # --- Test results ---

    def upsert_test_result(self, build_id: str, test_name: str,
                           test_variant: str, status: str, *,
                           suite: str | None = None,
                           duration_seconds: float | None = None,
                           is_leaf: bool = True,
                           failure_message: str | None = None,
                           _batch: bool = False) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO ci_test_results
               (build_id, test_name, suite, test_variant, status,
                duration_seconds, is_leaf, failure_message)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (build_id, test_name, suite, test_variant, status,
             duration_seconds, int(is_leaf), failure_message),
        )
        if not _batch:
            self.conn.commit()

    def get_test_results(self, build_id: str | None = None,
                         status: str | None = None,
                         leaf_only: bool = False) -> list[dict]:
        q = "SELECT * FROM ci_test_results WHERE 1=1"
        params: list[Any] = []
        if build_id:
            q += " AND build_id = ?"
            params.append(build_id)
        if status:
            q += " AND status = ?"
            params.append(status)
        if leaf_only:
            q += " AND is_leaf = 1"
        q += " ORDER BY build_id, test_variant, test_name"
        return [dict(r) for r in self.conn.execute(q, params).fetchall()]

    def get_all_test_results(self) -> list[dict]:
        """Get all test results, optimized for batch processing."""
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM ci_test_results ORDER BY build_id, test_variant, test_name"
        ).fetchall()]

    def test_result_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) as n FROM ci_test_results").fetchone()
        return row["n"] if row else 0

    def save_metric(self, metric: str, window: str, value: Any) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO metrics_cache (metric, window, value, computed_at) VALUES (?, ?, ?, ?)",
            (metric, window, json.dumps(value), datetime.utcnow().isoformat()),
        )
        self.conn.commit()

    def get_releases(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM releases ORDER BY published").fetchall()
        return [dict(r) for r in rows]

    def get_merged_prs(self, repo: str | None = None, base_branch: str | None = None) -> list[dict]:
        q = "SELECT * FROM merged_prs WHERE 1=1"
        params: list[Any] = []
        if repo:
            q += " AND repo = ?"
            params.append(repo)
        if base_branch:
            q += " AND base_branch = ?"
            params.append(base_branch)
        q += " ORDER BY merged_at"
        rows = self.conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]

    def get_reverts(self, repo: str | None = None) -> list[dict]:
        q = "SELECT * FROM reverts"
        params: list[Any] = []
        if repo:
            q += " WHERE repo = ?"
            params.append(repo)
        q += " ORDER BY date"
        return [dict(r) for r in self.conn.execute(q, params).fetchall()]

    def get_cherry_picks(self, repo: str | None = None) -> list[dict]:
        q = "SELECT * FROM cherry_picks"
        params: list[Any] = []
        if repo:
            q += " WHERE repo = ?"
            params.append(repo)
        q += " ORDER BY merged_at"
        return [dict(r) for r in self.conn.execute(q, params).fetchall()]

    def get_downstream_branches(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM downstream_branches ORDER BY first_commit_date"
        ).fetchall()]

    def get_branch_arrivals(self, pr_repo: str, pr_number: int) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM branch_arrivals WHERE pr_repo = ? AND pr_number = ?",
            (pr_repo, pr_number),
        ).fetchall()]

    def upsert_code_risk(self, repo: str, file: str, function: str,
                         component: str | None, complexity: float | None,
                         churn_30d: int | None, risk_score: float | None,
                         risk_band: str | None, analyzed_at: str) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO code_risk_scores
               (repo, file, function, component, complexity, churn_30d,
                risk_score, risk_band, analyzed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (repo, file, function, component, complexity, churn_30d,
             risk_score, risk_band, analyzed_at),
        )
        self.conn.commit()

    def get_code_risk_scores(self, repo: str | None = None,
                             component: str | None = None) -> list[dict]:
        q = "SELECT * FROM code_risk_scores WHERE 1=1"
        params: list[Any] = []
        if repo:
            q += " AND repo = ?"
            params.append(repo)
        if component:
            q += " AND component = ?"
            params.append(component)
        q += " ORDER BY risk_score DESC"
        return [dict(r) for r in self.conn.execute(q, params).fetchall()]

    def get_component_risk_summary(self) -> list[dict]:
        """Aggregate risk scores by component."""
        rows = self.conn.execute(
            """SELECT component,
                      COUNT(*) as total_functions,
                      SUM(CASE WHEN risk_band = 'Critical' THEN 1 ELSE 0 END) as critical,
                      SUM(CASE WHEN risk_band = 'High' THEN 1 ELSE 0 END) as high,
                      AVG(risk_score) as avg_risk
               FROM code_risk_scores
               WHERE component IS NOT NULL
               GROUP BY component
               ORDER BY avg_risk DESC"""
        ).fetchall()
        return [dict(r) for r in rows]

    # --- JIRA issues ---

    def upsert_jira_issue(self, issue: dict) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO jira_issues
               (key, summary, issue_type, priority, status, status_category,
                assignee, components, labels, fix_versions, story_points,
                created, resolved, epic_key, description, comments, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                issue["key"], issue.get("summary"), issue.get("issue_type"),
                issue.get("priority"), issue.get("status"), issue.get("status_category"),
                issue.get("assignee"), issue.get("components"), issue.get("labels"),
                issue.get("fix_versions"), issue.get("story_points"),
                issue.get("created"), issue.get("resolved"), issue.get("epic_key"),
                issue.get("description"), issue.get("comments"),
                issue["fetched_at"],
            ),
        )
        self.conn.commit()

    def get_jira_issues(self, keys: list[str] | None = None) -> list[dict]:
        if keys is not None:
            if not keys:
                return []
            placeholders = ",".join("?" for _ in keys)
            rows = self.conn.execute(
                f"SELECT * FROM jira_issues WHERE key IN ({placeholders}) ORDER BY key",
                keys,
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM jira_issues ORDER BY key").fetchall()
        return [dict(r) for r in rows]

    def get_jira_issue(self, key: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM jira_issues WHERE key = ?", (key,),
        ).fetchone()
        return dict(row) if row else None

    def get_jira_issue_map(self) -> dict[str, dict]:
        """Return a dict keyed by issue key for fast lookups."""
        return {row["key"]: row for row in self.get_jira_issues()}

    def get_fresh_jira_keys(self, max_age_hours: int = 4) -> set[str]:
        """Return issue keys whose fetched_at is within max_age_hours."""
        rows = self.conn.execute(
            """SELECT key FROM jira_issues
               WHERE fetched_at > datetime('now', ?)""",
            (f"-{max_age_hours} hours",),
        ).fetchall()
        return {r["key"] for r in rows}

    def set_collection_issues(self, collection_name: str, keys: list[str]) -> None:
        """Replace all membership for a collection."""
        self.conn.execute(
            "DELETE FROM jira_collection_issues WHERE collection_name = ?",
            (collection_name,),
        )
        for key in keys:
            self.conn.execute(
                "INSERT OR IGNORE INTO jira_collection_issues (collection_name, issue_key) VALUES (?, ?)",
                (collection_name, key),
            )
        self.conn.commit()

    def get_collection_issues(self, collection_name: str) -> list[dict]:
        """Return full jira_issues rows for a collection."""
        rows = self.conn.execute(
            """SELECT ji.* FROM jira_issues ji
               JOIN jira_collection_issues jci ON ji.key = jci.issue_key
               WHERE jci.collection_name = ?
               ORDER BY ji.key""",
            (collection_name,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_collection_names(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT collection_name FROM jira_collection_issues ORDER BY collection_name"
        ).fetchall()
        return [r["collection_name"] for r in rows]

    def get_metric(self, metric: str, window: str) -> Any | None:
        row = self.conn.execute(
            "SELECT value FROM metrics_cache WHERE metric = ? AND window = ?",
            (metric, window),
        ).fetchone()
        return json.loads(row["value"]) if row else None

    # --- AgentReady assessments ---

    def upsert_agentready_assessment(self, repo_url: str, project: str,
                                     overall_score: float, certification_level: str,
                                     attributes_assessed: int, attributes_total: int,
                                     findings_json: str, assessed_at: str) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO agentready_assessments
               (repo_url, project, overall_score, certification_level,
                attributes_assessed, attributes_total, findings_json, assessed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (repo_url, project, overall_score, certification_level,
             attributes_assessed, attributes_total, findings_json, assessed_at),
        )
        self.conn.commit()

    def get_agentready_assessments(self, project: str | None = None) -> list[dict]:
        q = "SELECT * FROM agentready_assessments"
        params: list[Any] = []
        if project:
            q += " WHERE project = ?"
            params.append(project)
        q += " ORDER BY project, repo_url"
        rows = self.conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]

    # --- Component manifest tracking ---

    def upsert_manifest_pin(self, component: str, repo_url: str,
                            branch: str | None, pinned_sha: str,
                            source_path: str | None, captured_at: str,
                            pr_number: int | None = None) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO component_manifest_pins
               (component, repo_url, branch, pinned_sha, source_path,
                captured_at, pr_number)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (component, repo_url, branch, pinned_sha, source_path,
             captured_at, pr_number),
        )
        self.conn.commit()

    def upsert_manifest_delta(self, component: str, old_sha: str, new_sha: str,
                               repo_url: str, commit_count: int,
                               commits_json: str, pr_number: int | None = None,
                               fetched_at: str | None = None) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO manifest_sha_deltas
               (component, old_sha, new_sha, repo_url, commit_count,
                commits_json, pr_number, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (component, old_sha, new_sha, repo_url, commit_count,
             commits_json, pr_number,
             fetched_at or datetime.utcnow().isoformat()),
        )
        self.conn.commit()

    def get_manifest_pins(self, component: str | None = None) -> list[dict]:
        q = "SELECT * FROM component_manifest_pins"
        params: list[Any] = []
        if component:
            q += " WHERE component = ?"
            params.append(component)
        q += " ORDER BY component, captured_at"
        return [dict(r) for r in self.conn.execute(q, params).fetchall()]

    def get_manifest_deltas(self, component: str | None = None) -> list[dict]:
        q = "SELECT * FROM manifest_sha_deltas"
        params: list[Any] = []
        if component:
            q += " WHERE component = ?"
            params.append(component)
        q += " ORDER BY component, fetched_at"
        return [dict(r) for r in self.conn.execute(q, params).fetchall()]

    def close(self) -> None:
        self.conn.close()
