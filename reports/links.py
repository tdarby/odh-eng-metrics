"""CI log and artifact link builder.

Constructs URLs to Prow build logs, GitHub PRs, and CI observability
Grafana dashboards for cross-referencing in reports and JSON exports.
"""

from __future__ import annotations

from dataclasses import dataclass, field


GCS_BUCKET = "test-platform-results"
GCS_BASE = "https://storage.googleapis.com"


@dataclass
class LinkBuilder:
    """Build URLs for CI artifacts given org/repo context and base URLs.

    All URL methods return plain strings suitable for markdown links or
    JSON values.  When ci_obs_grafana_url is empty, the ci_obs_* methods
    return None so callers can omit those links.
    """

    org: str
    repo: str
    prow_base: str = "https://prow.ci.openshift.org"
    ci_obs_grafana_url: str = "http://localhost:3000"

    def _gcs_build_prefix(self, pr_number: int, job_name: str, build_id: str) -> str:
        return (
            f"pr-logs/pull/{self.org}_{self.repo}"
            f"/{pr_number}/{job_name}/{build_id}"
        )

    def prow_build(self, pr_number: int, job_name: str, build_id: str) -> str:
        return (
            f"{self.prow_base}/view/gs/{GCS_BUCKET}/"
            f"{self._gcs_build_prefix(pr_number, job_name, build_id)}"
        )

    def gcs_artifacts(self, pr_number: int, job_name: str, build_id: str) -> str:
        """Direct link to the GCS artifacts directory for a build."""
        return (
            f"{GCS_BASE}/{GCS_BUCKET}/"
            f"{self._gcs_build_prefix(pr_number, job_name, build_id)}/artifacts/"
        )

    def gcs_build_log(self, pr_number: int, job_name: str, build_id: str) -> str:
        """Direct link to build-log.txt (raw ci-operator stdout)."""
        return (
            f"{GCS_BASE}/{GCS_BUCKET}/"
            f"{self._gcs_build_prefix(pr_number, job_name, build_id)}/build-log.txt"
        )

    def github_pr(self, pr_number: int) -> str:
        return f"https://github.com/{self.org}/{self.repo}/pull/{pr_number}"

    def ci_obs_logs(self, build_id: str) -> str | None:
        if not self.ci_obs_grafana_url:
            return None
        return f"{self.ci_obs_grafana_url}/d/ci-logs?var-build_id={build_id}"

    def ci_obs_investigation(self, build_id: str) -> str | None:
        if not self.ci_obs_grafana_url:
            return None
        return f"{self.ci_obs_grafana_url}/d/ci-investigation?var-build_id={build_id}"

    def ci_obs_tests(self, build_id: str) -> str | None:
        if not self.ci_obs_grafana_url:
            return None
        return f"{self.ci_obs_grafana_url}/d/ci-tests?var-build_id={build_id}"

    def ci_obs_pr_overview(self, pr_number: int) -> str | None:
        if not self.ci_obs_grafana_url:
            return None
        return f"{self.ci_obs_grafana_url}/d/ci-investigation?var-pr_number={pr_number}"


def from_config(cfg: dict) -> LinkBuilder:
    """Construct a LinkBuilder from the project config dict."""
    up = cfg.get("upstream", {})
    ci_obs = cfg.get("ci_observability", {})
    return LinkBuilder(
        org=up.get("owner", "opendatahub-io"),
        repo=up.get("repo", "opendatahub-operator"),
        ci_obs_grafana_url=ci_obs.get("grafana_url", "http://localhost:3000"),
    )


def local_access_appendix(lb: LinkBuilder) -> str:
    """Markdown appendix explaining how to access CI logs locally."""
    vm_base = lb.ci_obs_grafana_url.replace(":3000", ":8428")
    vl_base = lb.ci_obs_grafana_url.replace(":3000", ":9428")
    return f"""\
## Accessing CI Logs Locally

The Grafana links in this report point to a local **CI Observability** stack.
If the links don't load, the stack may not be running.

### Starting the stack

```bash
cd /path/to/openshift-ci-observability
make up          # starts VictoriaMetrics, VictoriaLogs, Grafana, and the scraper
make status      # verify all containers are healthy
```

The scraper backfills historical data automatically. After first start, wait
a few minutes for ingestion before querying.

### Grafana dashboards

| Dashboard | URL | Use for |
|-----------|-----|---------|
| CI Overview | {lb.ci_obs_grafana_url}/d/ci-overview | Health snapshot, success rates |
| CI Investigation | {lb.ci_obs_grafana_url}/d/ci-investigation | Drill into a build or PR |
| CI Logs | {lb.ci_obs_grafana_url}/d/ci-logs | Raw ci-operator log viewer |
| CI Tests | {lb.ci_obs_grafana_url}/d/ci-tests | JUnit test results and durations |

### Programmatic access with ci-query

The `ci-query` CLI tool queries VictoriaMetrics and VictoriaLogs and outputs
JSON lines.  It lives in the openshift-ci-observability repo.

```bash
# Health snapshot
ci-query --repo opendatahub-operator health

# Builds for a specific PR
ci-query --repo opendatahub-operator builds-for-pr 3346

# Step failures for a specific build
ci-query --repo opendatahub-operator step-failures <build_id>

# Search ci-operator logs for errors
ci-query --repo opendatahub-operator search-logs <build_id> error

# Full log timeline for a build
ci-query --repo opendatahub-operator all-logs <build_id>

# JUnit test results
ci-query --repo opendatahub-operator junit-tests <build_id>

# Top failing tests across all recent builds
ci-query --repo opendatahub-operator top-failing-tests

# Flakiness assessment for a PR
ci-query --repo opendatahub-operator flakiness <pr_number>
```

Run `ci-query help` for the full command list.

### Direct VictoriaLogs queries

For raw log searches beyond what ci-query provides:

```bash
# Search for error messages in a specific build
curl -s '{vl_base}/select/logsql/query' \\
  --data-urlencode 'query=build_id:"<build_id>" AND level:error' \\
  --data-urlencode 'limit=50'

# Find all failures for a PR
curl -s '{vl_base}/select/logsql/query' \\
  --data-urlencode 'query=pr_number:"<pr_number>" AND (level:error OR status:failed)' \\
  --data-urlencode 'limit=100'

# Search for a specific error pattern across all builds
curl -s '{vl_base}/select/logsql/query' \\
  --data-urlencode 'query=repo:"opendatahub-operator" AND _msg:"context deadline exceeded"' \\
  --data-urlencode 'limit=50'
```

### Direct VictoriaMetrics queries

```bash
# Step durations for a build
curl -s '{vm_base}/api/v1/query' \\
  --data-urlencode 'query=ci_step_duration_seconds{{build_id="<build_id>"}}'

# Failed steps for a build
curl -s '{vm_base}/api/v1/query' \\
  --data-urlencode 'query=ci_step_duration_seconds{{build_id="<build_id>",level="Error"}}'
```

### GCS artifacts (direct access)

Build artifacts are stored in the `{GCS_BUCKET}` GCS bucket.
Each build has a directory with raw logs, JUnit XML, and step artifacts.

```
{GCS_BASE}/{GCS_BUCKET}/pr-logs/pull/<org>_<repo>/<pr>/<job>/<build_id>/
├── build-log.txt          # raw ci-operator stdout/stderr
├── finished.json          # pass/fail result
├── started.json           # start timestamp
└── artifacts/             # per-step artifacts
    └── <step-name>/       # JUnit XML, must-gather, logs
```

```bash
# Download the raw build log
curl -o build-log.txt \\
  '{GCS_BASE}/{GCS_BUCKET}/pr-logs/pull/<org>_<repo>/<pr>/<job>/<build_id>/build-log.txt'

# List step artifacts
curl -s '{GCS_BASE}/{GCS_BUCKET}/?prefix=pr-logs/pull/<org>_<repo>/<pr>/<job>/<build_id>/artifacts/&delimiter=/'
```"""


def local_access_json(lb: LinkBuilder) -> dict:
    """Structured dict explaining how to access CI logs, for JSON exports."""
    return {
        "stack": "openshift-ci-observability",
        "start_command": "cd /path/to/openshift-ci-observability && make up",
        "grafana_url": lb.ci_obs_grafana_url,
        "dashboards": {
            "overview": f"{lb.ci_obs_grafana_url}/d/ci-overview",
            "investigation": f"{lb.ci_obs_grafana_url}/d/ci-investigation",
            "logs": f"{lb.ci_obs_grafana_url}/d/ci-logs",
            "tests": f"{lb.ci_obs_grafana_url}/d/ci-tests",
        },
        "gcs": {
            "bucket": GCS_BUCKET,
            "base_url": GCS_BASE,
            "path_pattern": (
                f"pr-logs/pull/{lb.org}_{lb.repo}"
                "/<pr_number>/<job_name>/<build_id>/"
            ),
            "key_files": [
                "build-log.txt",
                "finished.json",
                "started.json",
                "artifacts/<step>/",
            ],
            "note": (
                "GCS artifacts are publicly accessible. Use curl or gsutil "
                "to download build-log.txt, JUnit XML, or step artifacts directly."
            ),
        },
        "cli_tool": "ci-query",
        "cli_examples": {
            "health": "ci-query --repo opendatahub-operator health",
            "builds_for_pr": "ci-query --repo opendatahub-operator builds-for-pr <pr_number>",
            "step_failures": "ci-query --repo opendatahub-operator step-failures <build_id>",
            "search_logs": "ci-query --repo opendatahub-operator search-logs <build_id> error",
            "junit_tests": "ci-query --repo opendatahub-operator junit-tests <build_id>",
            "flakiness": "ci-query --repo opendatahub-operator flakiness <pr_number>",
        },
        "note": (
            "The Grafana links require the local CI Observability stack to be running. "
            "Run 'make up' in the openshift-ci-observability repo to start it. "
            "The scraper backfills data automatically on first start."
        ),
    }
