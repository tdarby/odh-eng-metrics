"""Orchestrate computation of all engineering metrics."""

from __future__ import annotations

import logging

from metrics import (
    ai_adoption,
    change_failure_rate,
    ci_efficiency,
    deployment_frequency,
    failure_analysis,
    git_ci_insights,
    lead_time,
    mttr,
    per_release,
    pipeline_velocity,
    pr_flow,
    throughput_over_time,
)
from store.db import Store

log = logging.getLogger(__name__)


def compute_all(store: Store, lookback_days: int = 365, min_version: str = "3.0.0") -> dict:
    """Compute all engineering metrics and return a unified result dict."""
    log.info("Computing Deployment Frequency...")
    df = deployment_frequency.compute(store, lookback_days=lookback_days)

    log.info("Computing Lead Time for Changes...")
    lt = lead_time.compute(store)

    log.info("Computing Change Failure Rate...")
    cfr = change_failure_rate.compute(store, lookback_days=lookback_days)

    log.info("Computing Mean Time to Recovery...")
    recovery = mttr.compute(store, lookback_days=lookback_days)

    log.info("Computing Per-Release Metrics (>= v%s)...", min_version)
    pr_data = per_release.compute(store, min_version=min_version)

    log.info("Computing Throughput Over Time...")
    throughput = throughput_over_time.compute(store)

    log.info("Computing Failure Analysis...")
    failures = failure_analysis.compute(store)

    log.info("Computing PR Flow...")
    flow = pr_flow.compute(store)

    log.info("Computing Pipeline Velocity...")
    pipeline = pipeline_velocity.compute(store, min_version=min_version)

    log.info("Computing AI Adoption...")
    ai = ai_adoption.compute(store)

    log.info("Computing CI Efficiency...")
    ci = ci_efficiency.compute(store)

    log.info("Computing Git-CI Insights...")
    git_ci = git_ci_insights.compute(store)

    result = {
        "deployment_frequency": df,
        "lead_time": lt,
        "change_failure_rate": cfr,
        "mttr": recovery,
        "per_release": pr_data,
        "throughput": throughput,
        "failure_analysis": failures,
        "pr_flow": flow,
        "pipeline_velocity": pipeline,
        "ai_adoption": ai,
        "ci_efficiency": ci,
        "git_ci_insights": git_ci,
        "summary": {
            "deployment_frequency": df["releases"]["dora_classification"],
            "lead_time": "See PR cycle time percentiles",
            "change_failure_rate": cfr["dora_classification"],
            "mttr": recovery["overall_recovery_hours"]["dora_classification"],
        },
    }

    store.save_metric("eng_all", "latest", result)
    return result
