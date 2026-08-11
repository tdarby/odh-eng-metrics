"""Tests for collector/code_analyzer.py."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from store.db import Store
from collector.code_analyzer import _risk_band, analyze_code_risk
from collector.pr_collector import _file_to_component


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as d:
        s = Store(Path(d) / "test.db")
        yield s
        s.close()


class TestRiskBand:
    def test_critical(self):
        assert _risk_band(9.0) == "Critical"
        assert _risk_band(10.0) == "Critical"

    def test_high(self):
        assert _risk_band(6.0) == "High"
        assert _risk_band(8.9) == "High"

    def test_medium(self):
        assert _risk_band(3.0) == "Medium"
        assert _risk_band(5.9) == "Medium"

    def test_low(self):
        assert _risk_band(0.0) == "Low"
        assert _risk_band(2.9) == "Low"


class TestFileToComponent:
    def test_controller_component(self):
        assert _file_to_component("internal/controller/components/kserve/controller.go") == "kserve"
        assert _file_to_component("internal/controller/components/dashboard/setup.go") == "dashboard"
        assert _file_to_component("internal/controller/components/ray/actions.go") == "ray"

    def test_api_types(self):
        assert _file_to_component("api/components/v1alpha1/kserve_types.go") == "kserve"
        assert _file_to_component("api/components/v1alpha1/dashboard_types.go") == "dashboard"

    def test_e2e_tests(self):
        assert _file_to_component("tests/e2e/kserve_test.go") == "kserve"
        assert _file_to_component("tests/e2e/trustyai_test.go") == "trustyai"

    def test_pkg_is_core_framework(self):
        assert _file_to_component("pkg/controller/actions/foo.go") == "core-framework"

    def test_config_crd(self):
        assert _file_to_component("config/crd/bases/foo.yaml") == "crd-config"

    def test_unknown_file(self):
        assert _file_to_component("README.md") is None
        assert _file_to_component("go.mod") is None

    def test_unknown_component_in_controller(self):
        assert _file_to_component("internal/controller/components/nonexistent/foo.go") is None

    def test_all_known_components(self):
        from collector.pr_collector import KNOWN_COMPONENTS
        for comp in KNOWN_COMPONENTS:
            path = f"internal/controller/components/{comp}/controller.go"
            assert _file_to_component(path) == comp, f"Failed for {comp}"


class TestAnalyzeCodeRisk:
    def test_no_tools_available(self, store):
        """When neither hotspots nor gocyclo is installed, returns 0."""
        count = analyze_code_risk(store, Path("/nonexistent"), "test/repo")
        assert count == 0

    def test_store_methods(self, store):
        """Verify the store can round-trip code risk data."""
        store.upsert_code_risk(
            repo="test/repo",
            file="internal/controller/components/kserve/ctrl.go",
            function="Reconcile",
            component="kserve",
            complexity=15.0,
            churn_30d=10,
            risk_score=9.5,
            risk_band="Critical",
            analyzed_at="2025-01-01T00:00:00Z",
        )
        scores = store.get_code_risk_scores(repo="test/repo")
        assert len(scores) == 1
        assert scores[0]["function"] == "Reconcile"
        assert scores[0]["component"] == "kserve"
        assert scores[0]["risk_band"] == "Critical"

    def test_component_risk_summary(self, store):
        store.upsert_code_risk("test/repo", "a.go", "f1", "kserve", 15.0, 10, 9.5, "Critical", "2025-01-01")
        store.upsert_code_risk("test/repo", "b.go", "f2", "kserve", 8.0, 5, 7.0, "High", "2025-01-01")
        store.upsert_code_risk("test/repo", "c.go", "f3", "dashboard", 3.0, 2, 2.0, "Low", "2025-01-01")

        summary = store.get_component_risk_summary()
        assert len(summary) == 2
        kserve = next(s for s in summary if s["component"] == "kserve")
        assert kserve["critical"] == 1
        assert kserve["high"] == 1
        assert kserve["total_functions"] == 2
