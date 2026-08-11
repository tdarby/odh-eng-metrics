"""Tests for metrics/git_ci_insights.py -- Engineering Intelligence metrics."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from store.db import Store
from metrics import git_ci_insights


@pytest.fixture
def store():
    """In-memory store for testing."""
    with tempfile.TemporaryDirectory() as d:
        s = Store(Path(d) / "test.db")
        yield s
        s.close()


def _add_pr(store, number, components=None, jira_keys=None, is_ai=False, merge_sha=None):
    store.upsert_pr("test/repo", {
        "number": number,
        "title": f"PR #{number}",
        "author": "dev",
        "created_at": "2025-01-01T00:00:00Z",
        "merged_at": "2025-01-02T00:00:00Z",
        "first_commit_at": "2025-01-01T00:00:00Z",
        "base_branch": "main",
        "jira_keys": jira_keys or [],
        "changed_components": components or [],
        "is_ai_assisted": is_ai,
        "merge_sha": merge_sha or f"abc{number}",
    })


def _add_build(store, build_id, pr_number, result="success", job="e2e"):
    store.upsert_ci_build(
        build_id=build_id,
        pr_number=pr_number,
        job_name=job,
        duration_seconds=600.0,
        result=result,
        started_at="2025-01-02T00:00:00Z",
    )


# --- Component CI Health ---

class TestComponentCIHealth:
    def test_groups_by_component(self, store):
        _add_pr(store, 1, components=["kserve"])
        _add_pr(store, 2, components=["dashboard"])
        _add_build(store, "b1", 1, "failure")
        _add_build(store, "b2", 2, "success")

        results = git_ci_insights.compute_component_ci_health(store)
        names = [r["component"] for r in results]
        assert "kserve" in names
        assert "dashboard" in names

    def test_pr_touching_multiple_components(self, store):
        _add_pr(store, 1, components=["kserve", "dashboard"])
        _add_build(store, "b1", 1, "failure")

        results = git_ci_insights.compute_component_ci_health(store)
        assert len(results) == 2
        for r in results:
            assert r.get("total_prs_with_ci") == 1

    def test_empty_builds(self, store):
        _add_pr(store, 1, components=["kserve"])
        results = git_ci_insights.compute_component_ci_health(store)
        assert results == []


# --- Code Hotspot Correlation ---

class TestCodeHotspotCorrelation:
    def test_no_risk_data(self, store):
        result = git_ci_insights.compute_code_hotspot_correlation(store)
        assert result["available"] is False

    def test_correlation_with_risk_data(self, store):
        store.upsert_code_risk("test/repo", "internal/controller/components/kserve/ctrl.go",
                               "Reconcile", "kserve", 15.0, 10, 9.5, "Critical", "2025-01-01")
        _add_pr(store, 1, components=["kserve"])
        store.upsert_pr("test/repo", {
            "number": 1, "title": "PR #1", "author": "dev",
            "created_at": "2025-01-01T00:00:00Z", "merged_at": "2025-01-02T00:00:00Z",
            "first_commit_at": "2025-01-01T00:00:00Z", "base_branch": "main",
            "changed_files": ["internal/controller/components/kserve/ctrl.go"],
            "changed_components": ["kserve"],
        })
        _add_build(store, "b1", 1, "failure")

        result = git_ci_insights.compute_code_hotspot_correlation(store)
        assert result["available"] is True
        critical = next(b for b in result["by_risk_band"] if b["risk_band"] == "Critical")
        assert critical["pr_count"] == 1


# --- AI CI Summary ---

class TestAICISummary:
    def test_no_builds(self, store):
        result = git_ci_insights.compute_ai_ci_summary(store)
        assert result["available"] is False

    def test_ai_summary(self, store):
        _add_pr(store, 1, is_ai=True)
        _add_pr(store, 2, is_ai=False)
        _add_build(store, "b1", 1, "failure")
        _add_build(store, "b2", 2, "success")

        result = git_ci_insights.compute_ai_ci_summary(store)
        assert result["available"] is True
        assert result["ai_pr_count"] == 1
        assert result["total_pr_count"] == 2


# --- Jira CI Health ---

class TestJiraCIHealth:
    def test_groups_by_jira(self, store):
        _add_pr(store, 1, jira_keys=["RHOAIENG-100"])
        _add_pr(store, 2, jira_keys=["RHOAIENG-200"])
        _add_build(store, "b1", 1, "failure")
        _add_build(store, "b2", 2, "success")

        results = git_ci_insights.compute_jira_ci_health(store)
        keys = [r["jira_key"] for r in results]
        assert "RHOAIENG-100" in keys
        assert "RHOAIENG-200" in keys

    def test_pr_with_multiple_jira_keys(self, store):
        _add_pr(store, 1, jira_keys=["RHOAIENG-100", "RHOAIENG-200"])
        _add_build(store, "b1", 1, "failure")

        results = git_ci_insights.compute_jira_ci_health(store)
        assert len(results) == 2

    def test_empty_jira(self, store):
        _add_pr(store, 1)
        _add_build(store, "b1", 1, "failure")
        results = git_ci_insights.compute_jira_ci_health(store)
        assert results == []


# --- Release CI Health ---

class TestReleaseCIHealth:
    def test_no_releases(self, store):
        results = git_ci_insights.compute_release_ci_health(store)
        assert results == []

    def test_release_with_arrivals(self, store):
        store.upsert_release("v3.5.0", "2025-01-10", prerelease=False, is_patch=False, is_ea=False)
        _add_pr(store, 1)
        _add_build(store, "b1", 1, "failure")
        store.upsert_branch_arrival("test/repo", 1, "v3.5.0", "2025-01-05")

        results = git_ci_insights.compute_release_ci_health(store)
        assert len(results) == 1
        assert results[0]["release"] == "v3.5.0"


# --- Revert Signals ---

class TestRevertSignals:
    def test_no_reverts(self, store):
        result = git_ci_insights.compute_revert_signals(store)
        assert result["total_reverts_with_pr"] == 0

    def test_revert_with_ci_failure(self, store):
        _add_pr(store, 1)
        _add_build(store, "b1", 1, "failure")
        store.upsert_revert("test/repo", "rev1", "2025-01-05", "abc1",
                            "Revert \"something\"", reverted_pr=1)

        result = git_ci_insights.compute_revert_signals(store)
        assert result["total_reverts_with_pr"] == 1
        assert result["ci_warned_pct"] == 100.0
        assert result["details"][0]["ci_had_failures"] is True

    def test_revert_without_ci_failure(self, store):
        _add_pr(store, 1)
        _add_build(store, "b1", 1, "success")
        store.upsert_revert("test/repo", "rev1", "2025-01-05", "abc1",
                            "Revert \"something\"", reverted_pr=1)

        result = git_ci_insights.compute_revert_signals(store)
        assert result["ci_warned_pct"] == 0


# --- Component Resource Cost ---

class TestComponentResourceCost:
    def test_resource_aggregation(self, store):
        _add_pr(store, 1, components=["kserve"])
        store.upsert_ci_build(
            build_id="b1", pr_number=1, job_name="e2e",
            duration_seconds=3600.0, result="success",
            started_at="2025-01-02T00:00:00Z",
            peak_cpu_cores=4.0, peak_memory_bytes=8 * 1024**3,
        )

        results = git_ci_insights.compute_component_resource_cost(store)
        assert len(results) == 1
        assert results[0]["component"] == "kserve"
        assert results[0]["cpu_hours"] == 4.0
        assert results[0]["memory_gb_hours"] == 8.0


def _add_build_step(store, build_id, step_name, duration=60.0, level="Info", is_infra=False):
    store.upsert_build_step(build_id, step_name, duration, level, is_infra)


def _add_failure_message(store, build_id, message, source="junit_step", count=1):
    store.upsert_build_failure_message(build_id, message, source, count)


# --- Component Step Breakdown ---

class TestComponentStepBreakdown:
    def test_groups_step_failures_by_component(self, store):
        _add_pr(store, 1, components=["kserve"])
        _add_pr(store, 2, components=["dashboard"])
        _add_build(store, "b1", 1, "failure")
        _add_build(store, "b2", 2, "failure")
        _add_build_step(store, "b1", "rhoai-e2e", level="Error")
        _add_build_step(store, "b1", "ipi-install-install", level="Error", is_infra=True)
        _add_build_step(store, "b2", "e2e", level="Error")

        results = git_ci_insights.compute_component_step_breakdown(store)
        kserve = next(r for r in results if r["component"] == "kserve")
        assert kserve["total_failures"] == 2
        step_names = [s["step"] for s in kserve["steps"]]
        assert "rhoai-e2e" in step_names
        assert "ipi-install-install" in step_names

        dashboard = next(r for r in results if r["component"] == "dashboard")
        assert dashboard["total_failures"] == 1

    def test_empty_steps(self, store):
        _add_pr(store, 1, components=["kserve"])
        _add_build(store, "b1", 1, "failure")
        results = git_ci_insights.compute_component_step_breakdown(store)
        assert results == []

    def test_percentage_calculation(self, store):
        _add_pr(store, 1, components=["kserve"])
        _add_build(store, "b1", 1, "failure")
        _add_build(store, "b2", 1, "failure")
        _add_build_step(store, "b1", "e2e", level="Error")
        _add_build_step(store, "b2", "e2e", level="Error")
        _add_build_step(store, "b2", "ipi-install", level="Error", is_infra=True)

        results = git_ci_insights.compute_component_step_breakdown(store)
        kserve = results[0]
        e2e = next(s for s in kserve["steps"] if s["step"] == "e2e")
        assert e2e["failures"] == 2
        assert e2e["pct"] == pytest.approx(66.7, abs=0.1)


# --- Cycle Duration Breakdown ---

class TestCycleDurationBreakdown:
    def test_breaks_down_by_category(self, store):
        _add_pr(store, 1, components=["ray"])
        _add_build(store, "b1", 1, "success")
        _add_build_step(store, "b1", "ipi-install-install", duration=1200.0, is_infra=True)
        _add_build_step(store, "b1", "rhoai-e2e", duration=2400.0, is_infra=False)

        results = git_ci_insights.compute_cycle_duration_breakdown(store)
        assert len(results) == 1
        ray = results[0]
        assert ray["component"] == "ray"
        assert ray["avg_total_min"] == 60.0

        cats = {c["category"]: c for c in ray["breakdown"]}
        assert "provisioning" in cats
        assert "test_execution" in cats
        assert cats["provisioning"]["avg_min"] == 20.0
        assert cats["test_execution"]["avg_min"] == 40.0

    def test_empty_steps(self, store):
        results = git_ci_insights.compute_cycle_duration_breakdown(store)
        assert results == []


# --- Infra vs Code Failures ---

class TestInfraVsCodeFailures:
    def test_classifies_infra_failures(self, store):
        _add_pr(store, 1, components=["kserve"])
        _add_build(store, "b1", 1, "failure")
        _add_build(store, "b2", 1, "failure")
        _add_build_step(store, "b1", "ipi-install-install", level="Error", is_infra=True)
        _add_build_step(store, "b2", "e2e", level="Error", is_infra=False)

        results = git_ci_insights.compute_infra_vs_code_failures(store)
        assert len(results) == 1
        kserve = results[0]
        assert kserve["total_failures"] == 2
        assert kserve["infra_failures"] == 1
        assert kserve["code_failures"] == 1
        assert kserve["infra_pct"] == 50.0

    def test_empty_data(self, store):
        results = git_ci_insights.compute_infra_vs_code_failures(store)
        assert results == []

    def test_only_code_failures(self, store):
        _add_pr(store, 1, components=["dashboard"])
        _add_build(store, "b1", 1, "failure")
        _add_build_step(store, "b1", "e2e", level="Error", is_infra=False)

        results = git_ci_insights.compute_infra_vs_code_failures(store)
        assert results[0]["infra_pct"] == 0
        assert results[0]["code_pct"] == 100.0


# --- Component Failure Reasons ---

class TestComponentFailureReasons:
    def test_top_3_reasons(self, store):
        _add_pr(store, 1, components=["kserve"])
        _add_build(store, "b1", 1, "failure")
        _add_failure_message(store, "b1", "timed out waiting for condition", count=5)
        _add_failure_message(store, "b1", "expected 200 got 503", count=3)
        _add_failure_message(store, "b1", "context deadline exceeded", count=2)
        _add_failure_message(store, "b1", "rare error", count=1)

        results = git_ci_insights.compute_component_failure_reasons(store)
        assert len(results) == 1
        reasons = results[0]["top_reasons"]
        assert len(reasons) == 3
        assert reasons[0]["message"] == "timed out waiting for condition"
        assert reasons[0]["count"] == 5
        assert reasons[1]["count"] == 3

    def test_empty_messages(self, store):
        results = git_ci_insights.compute_component_failure_reasons(store)
        assert results == []


# --- Weekly Component Failures ---

class TestWeeklyComponentFailures:
    def test_groups_by_week_and_component(self, store):
        _add_pr(store, 1, components=["kserve"])
        _add_pr(store, 2, components=["dashboard"])
        store.upsert_ci_build("b1", 1, "e2e", 600.0, "failure", "2025-01-06T00:00:00Z")
        store.upsert_ci_build("b2", 2, "e2e", 600.0, "failure", "2025-01-06T00:00:00Z")
        store.upsert_ci_build("b3", 1, "rhoai-e2e", 600.0, "failure", "2025-01-06T00:00:00Z")

        results = git_ci_insights.compute_weekly_component_failures(store)
        assert len(results) >= 2
        kserve_rows = [r for r in results if r["component"] == "kserve"]
        assert kserve_rows[0]["failures"] >= 1

    def test_no_builds(self, store):
        results = git_ci_insights.compute_weekly_component_failures(store)
        assert results == []


# --- Full compute ---

class TestFullCompute:
    def test_compute_returns_all_sections(self, store):
        _add_pr(store, 1, components=["kserve"], jira_keys=["RHOAIENG-100"])
        _add_build(store, "b1", 1, "failure")
        _add_build_step(store, "b1", "e2e", level="Error")
        _add_failure_message(store, "b1", "some error")

        result = git_ci_insights.compute(store)
        assert "component_health" in result
        assert "code_hotspots" in result
        assert "component_resource_cost" in result
        assert "ai_summary" in result
        assert "jira_health" in result
        assert "release_health" in result
        assert "revert_signals" in result
        assert "step_breakdown" in result
        assert "cycle_duration_breakdown" in result
        assert "infra_vs_code" in result
        assert "failure_reasons" in result
        assert "jira_failure_reasons" in result
        assert "weekly_component_failures" in result
