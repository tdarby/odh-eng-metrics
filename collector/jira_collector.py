"""Collect JIRA issue metadata via the REST API.

Two collection modes:
  1. PR-referenced issues — fetch metadata for JIRA keys found in merged PRs
  2. Label-based collections — independently query for issue sets by label

Set JIRA_TOKEN env var with a Personal Access Token for authentication.

Atlassian Cloud uses REST API v3 (``/rest/api/3/search/jql``) with
token-based pagination.  JIRA Server/Data Center uses REST API v2
(``/rest/api/2/search``) with offset-based pagination.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from store.db import Store

log = logging.getLogger(__name__)

MAX_JQL_CHUNK = 50
SEARCH_PAGE_SIZE = 50
REQUEST_DELAY_SECONDS = 0.1  # 100ms between requests to avoid bursting
MAX_RETRIES = 3
INITIAL_BACKOFF = 2  # seconds; doubles on each retry

STANDARD_FIELDS = [
    "summary", "description", "issuetype", "priority", "status", "assignee",
    "components", "labels", "fixVersions", "created", "resolutiondate",
    "parent",
]


def _rate_limited_request(
    client: httpx.Client,
    method: str,
    url: str,
    **kwargs: Any,
) -> httpx.Response:
    """Execute an HTTP request with rate-limit retry and inter-request throttling.

    Retries on 429 (Too Many Requests) with exponential backoff, respecting
    the Retry-After header when present.
    """
    time.sleep(REQUEST_DELAY_SECONDS)
    backoff = INITIAL_BACKOFF

    for attempt in range(MAX_RETRIES + 1):
        resp = client.request(method, url, **kwargs)
        if resp.status_code != 429:
            return resp

        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                wait = int(retry_after)
            except ValueError:
                wait = backoff
        else:
            wait = backoff

        if attempt == MAX_RETRIES:
            log.warning("JIRA rate limit: giving up after %d retries for %s", MAX_RETRIES, url)
            return resp

        log.info("JIRA rate limit (429): retrying in %ds (attempt %d/%d)", wait, attempt + 1, MAX_RETRIES)
        time.sleep(wait)
        backoff *= 2

    return resp  # unreachable, but satisfies type checker


def _is_cloud(cfg: dict) -> bool:
    base_url = cfg.get("jira", {}).get("base_url", "https://redhat.atlassian.net")
    return "atlassian.net" in base_url


def _api_prefix(cfg: dict) -> str:
    """Return ``/rest/api/3`` for Cloud, ``/rest/api/2`` for Server/DC."""
    return "/rest/api/3" if _is_cloud(cfg) else "/rest/api/2"


def _build_client(cfg: dict) -> httpx.Client | None:
    """Create an authenticated httpx client, or None if no credentials.

    Atlassian Cloud (*.atlassian.net) uses Basic auth with email + API token.
    Set JIRA_EMAIL and JIRA_TOKEN.

    JIRA Server/Data Center uses Bearer token auth.
    Set JIRA_TOKEN only.
    """
    token = os.environ.get("JIRA_TOKEN")
    if not token:
        log.warning(
            "JIRA_TOKEN not set — skipping JIRA collection. "
            "Set it to enable JIRA enrichment."
        )
        return None

    base_url = cfg.get("jira", {}).get("base_url", "https://redhat.atlassian.net")
    is_cloud = "atlassian.net" in base_url

    if is_cloud:
        email = os.environ.get("JIRA_EMAIL")
        if not email:
            log.warning(
                "JIRA_EMAIL not set — required for Atlassian Cloud (%s). "
                "Set it to the email associated with your API token.",
                base_url,
            )
            return None
        credentials = base64.b64encode(f"{email}:{token}".encode()).decode()
        auth_header = f"Basic {credentials}"
    else:
        auth_header = f"Bearer {token}"

    return httpx.Client(
        base_url=base_url.rstrip("/"),
        headers={
            "Authorization": auth_header,
            "Accept": "application/json",
        },
        timeout=30,
    )


def _requested_fields(cfg: dict) -> list[str]:
    """Build the field list including optional custom fields."""
    jira_cfg = cfg.get("jira", {})
    fields = list(STANDARD_FIELDS)
    sp_field = jira_cfg.get("story_points_field")
    if sp_field:
        fields.append(sp_field)
    epic_field = jira_cfg.get("epic_link_field")
    if epic_field:
        fields.append(epic_field)
    return fields


def _adf_to_text(node: Any) -> str:
    """Recursively extract plain text from an Atlassian Document Format tree.

    ADF is a JSON structure used by JIRA Cloud v3 for rich text fields.
    Returns a best-effort plain text rendering.
    """
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ""
    text_parts: list[str] = []
    if node.get("type") == "text":
        text_parts.append(node.get("text", ""))
    for child in node.get("content", []):
        text_parts.append(_adf_to_text(child))
    return "\n".join(part for part in text_parts if part) if text_parts else ""


def _extract_text_field(value: Any) -> str | None:
    """Normalise a text field that may be plain text (v2) or ADF (v3)."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return _adf_to_text(value) or None
    return None


def _fetch_comments(client: httpx.Client, issue_key: str, cfg: dict) -> list[dict]:
    """Fetch all comments for an issue via the REST API."""
    api = _api_prefix(cfg)
    try:
        resp = _rate_limited_request(client, "GET", f"{api}/issue/{issue_key}/comment")
        if resp.status_code != 200:
            log.debug("Failed to fetch comments for %s (%d)", issue_key, resp.status_code)
            return []
        data = resp.json()
        return [
            {
                "author": (c.get("author") or {}).get("displayName", "Unknown"),
                "body": _extract_text_field(c.get("body")) or "",
                "created": c.get("created", ""),
            }
            for c in data.get("comments", [])
        ]
    except Exception:
        log.debug("Error fetching comments for %s", issue_key, exc_info=True)
        return []


def _extract_issue(raw: dict, cfg: dict, comments: list[dict] | None = None) -> dict:
    """Extract a flat dict from a JIRA issue JSON response."""
    fields = raw.get("fields", {})
    jira_cfg = cfg.get("jira", {})

    assignee = fields.get("assignee")
    components = [c["name"] for c in (fields.get("components") or [])]
    fix_versions = [v["name"] for v in (fields.get("fixVersions") or [])]
    labels = fields.get("labels") or []

    status = fields.get("status") or {}
    status_cat = (status.get("statusCategory") or {}).get("name")

    story_points = None
    sp_field = jira_cfg.get("story_points_field")
    if sp_field:
        story_points = fields.get(sp_field)

    epic_key = None
    epic_field = jira_cfg.get("epic_link_field")
    if epic_field:
        epic_key = fields.get(epic_field)
    if not epic_key:
        parent = fields.get("parent")
        if parent:
            epic_key = parent.get("key")

    return {
        "key": raw["key"],
        "summary": fields.get("summary"),
        "description": _extract_text_field(fields.get("description")),
        "issue_type": (fields.get("issuetype") or {}).get("name"),
        "priority": (fields.get("priority") or {}).get("name"),
        "status": status.get("name"),
        "status_category": status_cat,
        "assignee": assignee.get("displayName") if assignee else None,
        "components": json.dumps(components),
        "labels": json.dumps(labels),
        "fix_versions": json.dumps(fix_versions),
        "story_points": story_points,
        "created": fields.get("created"),
        "resolved": fields.get("resolutiondate"),
        "epic_key": epic_key,
        "comments": json.dumps(comments) if comments is not None else None,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def _search_issues(
    client: httpx.Client, jql: str, fields: list[str], cfg: dict | None = None,
) -> list[dict]:
    """Execute a JQL search with pagination, returning raw issue dicts.

    Atlassian Cloud (v3): POST ``/rest/api/3/search/jql`` with token pagination.
    JIRA Server/DC (v2): POST ``/rest/api/2/search`` with offset pagination.
    """
    cloud = _is_cloud(cfg or {})
    if cloud:
        return _search_issues_v3(client, jql, fields)
    return _search_issues_v2(client, jql, fields)


def _search_issues_v3(
    client: httpx.Client, jql: str, fields: list[str],
) -> list[dict]:
    """Cloud v3 search via ``/rest/api/3/search/jql`` with nextPageToken."""
    results: list[dict] = []
    next_token: str | None = None

    while True:
        body: dict[str, Any] = {
            "jql": jql,
            "fields": fields,
            "maxResults": SEARCH_PAGE_SIZE,
        }
        if next_token:
            body["nextPageToken"] = next_token

        resp = _rate_limited_request(client, "POST", "/rest/api/3/search/jql", json=body)
        if resp.status_code == 401:
            log.error("JIRA authentication failed (401). Check JIRA_TOKEN / JIRA_EMAIL.")
            return results
        if resp.status_code == 400:
            log.error("JIRA query failed (400): %s — JQL: %s", resp.text[:200], jql)
            return results
        resp.raise_for_status()

        data = resp.json()
        issues = data.get("issues", [])
        results.extend(issues)

        if data.get("isLast", True) or not issues:
            break
        next_token = data.get("nextPageToken")
        if not next_token:
            break

    return results


def _search_issues_v2(
    client: httpx.Client, jql: str, fields: list[str],
) -> list[dict]:
    """Server/DC v2 search via ``/rest/api/2/search`` with startAt offset."""
    results: list[dict] = []
    start_at = 0

    while True:
        resp = _rate_limited_request(
            client, "POST", "/rest/api/2/search",
            json={
                "jql": jql,
                "fields": fields,
                "startAt": start_at,
                "maxResults": SEARCH_PAGE_SIZE,
            },
        )
        if resp.status_code == 401:
            log.error("JIRA authentication failed (401). Check JIRA_TOKEN.")
            return results
        if resp.status_code == 400:
            log.error("JIRA query failed (400): %s — JQL: %s", resp.text[:200], jql)
            return results
        resp.raise_for_status()

        data = resp.json()
        issues = data.get("issues", [])
        results.extend(issues)

        total = data.get("total", 0)
        start_at += len(issues)
        if start_at >= total or not issues:
            break

    return results


def _unique_jira_keys_from_prs(store: Store) -> set[str]:
    """Extract all unique JIRA keys referenced in merged PRs."""
    prs = store.get_merged_prs()
    keys: set[str] = set()
    for pr in prs:
        raw = pr.get("jira_keys")
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
            keys.update(parsed)
        except (json.JSONDecodeError, TypeError):
            pass
    return keys


def collect_pr_issues(store: Store, cfg: dict) -> int:
    """Fetch JIRA metadata for all issue keys referenced in merged PRs.

    Incremental: skips keys already present in jira_issues.
    Returns the number of issues fetched.
    """
    client = _build_client(cfg)
    if client is None:
        return 0

    all_keys = _unique_jira_keys_from_prs(store)
    if not all_keys:
        log.info("No JIRA keys found in merged PRs")
        client.close()
        return 0

    existing = {row["key"] for row in store.get_jira_issues()}
    new_keys = sorted(all_keys - existing)

    if not new_keys:
        log.info("All %d JIRA keys already fetched", len(all_keys))
        client.close()
        return 0

    total = len(new_keys)
    log.info("Fetching %d new JIRA issues (of %d total referenced)", total, len(all_keys))
    fields = _requested_fields(cfg)
    fetched = 0

    try:
        for i in range(0, total, MAX_JQL_CHUNK):
            chunk = new_keys[i : i + MAX_JQL_CHUNK]
            key_list = ", ".join(chunk)
            jql = f"key in ({key_list})"
            issues = _search_issues(client, jql, fields, cfg=cfg)

            for raw in issues:
                comments = _fetch_comments(client, raw["key"], cfg)
                extracted = _extract_issue(raw, cfg, comments=comments)
                store.upsert_jira_issue(extracted)
                fetched += 1

            log.info("  PR issues: %d/%d fetched", fetched, total)
    finally:
        client.close()

    return fetched


def _discover_labels_by_prefix(client: httpx.Client, prefix: str, cfg: dict | None = None) -> list[str]:
    """Use JIRA's autocomplete API to find all labels matching a prefix.

    Returns the list of matching label strings, or an empty list on failure.
    """
    api = _api_prefix(cfg or {})
    resp = _rate_limited_request(
        client, "GET", f"{api}/jql/autocompletedata/suggestions",
        params={"fieldName": "labels", "fieldValue": prefix},
    )
    if resp.status_code != 200:
        log.warning(
            "Label autocomplete failed (%d), falling back to empty list. "
            "You may need to list labels explicitly instead of using label_prefix.",
            resp.status_code,
        )
        return []

    data = resp.json()
    results = data.get("results", [])
    # Each result has a "value" field with the label name.
    # Filter to those actually starting with the prefix (the API does
    # substring matching, not just prefix matching).
    labels = [
        r["value"] for r in results
        if r.get("value", "").startswith(prefix)
    ]
    log.info("Label prefix '%s' resolved to %d labels: %s", prefix, len(labels), labels)
    return labels


def _project_clause(cfg: dict, collection: dict) -> str:
    """Build the project filter for a collection's JQL.

    If the collection defines ``projects`` (a list), uses
    ``project in ("A", "B", ...)``.  Otherwise falls back to the
    top-level ``jira.project`` as ``project = X``.
    """
    projects = collection.get("projects")
    if projects and len(projects) > 1:
        proj_list = ", ".join(f'"{p}"' for p in projects)
        return f"project in ({proj_list})"
    if projects:
        return f"project = {projects[0]}"
    return f"project = {cfg.get('jira', {}).get('project', 'RHOAIENG')}"


def _build_collection_jql(
    cfg: dict, collection: dict, client: httpx.Client | None = None,
) -> str | None:
    """Build the JQL query for a collection.

    Supports three modes (checked in order):
      1. ``jql`` — freeform JQL string (most flexible, used as-is)
      2. ``label_prefix`` — discovers matching labels via the JIRA
         autocomplete API, then uses ``labels in (...)``
      3. ``labels`` — explicit label list via ``labels in (...)``

    Collections can optionally specify ``projects`` (list) to search
    across multiple JIRA projects instead of the default single project.
    """
    proj = _project_clause(cfg, collection)

    if collection.get("jql"):
        return collection["jql"]

    if collection.get("label_prefix"):
        prefix = collection["label_prefix"]
        if client is None:
            log.error("label_prefix requires a JIRA client for label discovery")
            return None
        discovered = _discover_labels_by_prefix(client, prefix, cfg=cfg)
        if not discovered:
            log.warning(
                "No labels found matching prefix '%s'. "
                "Check the prefix or list labels explicitly.",
                prefix,
            )
            return None
        label_list = ", ".join(f'"{lbl}"' for lbl in discovered)
        return f"{proj} AND labels in ({label_list})"

    labels = collection.get("labels", [])
    if not labels:
        return None

    label_list = ", ".join(f'"{lbl}"' for lbl in labels)
    return f"{proj} AND labels in ({label_list})"


COLLECTION_FRESHNESS_HOURS = 4


def collect_collection(store: Store, cfg: dict, collection: dict) -> int:
    """Fetch all JIRA issues matching a collection definition.

    Supports ``labels``, ``label_prefix``, or freeform ``jql``.
    Collection membership is always refreshed (the JQL search runs every time),
    but individual issue details and comments are only re-fetched if the cached
    copy is older than COLLECTION_FRESHNESS_HOURS.
    Returns the number of issues in the collection.
    """
    client = _build_client(cfg)
    if client is None:
        return 0

    name = collection["name"]
    jql = _build_collection_jql(cfg, collection, client=client)
    if not jql:
        log.warning("Collection '%s': no matching query could be built — skipping", name)
        client.close()
        return 0

    log.info("Collecting '%s': %s", name, jql)
    fields = _requested_fields(cfg)
    fresh_keys = store.get_fresh_jira_keys(max_age_hours=COLLECTION_FRESHNESS_HOURS)

    try:
        issues = _search_issues(client, jql, fields, cfg=cfg)
        total = len(issues)
        log.info("  '%s': found %d issues, checking freshness...", name, total)

        keys: list[str] = []
        fetched = 0
        skipped = 0
        for idx, raw in enumerate(issues, 1):
            key = raw["key"]
            keys.append(key)
            if key in fresh_keys:
                skipped += 1
            else:
                comments = _fetch_comments(client, raw["key"], cfg)
                extracted = _extract_issue(raw, cfg, comments=comments)
                store.upsert_jira_issue(extracted)
                fetched += 1
            if idx % 25 == 0 or idx == total:
                log.info("  '%s': %d/%d processed (%d fetched, %d cached)", name, idx, total, fetched, skipped)
    finally:
        client.close()

    store.set_collection_issues(name, keys)

    if collection.get("baseline_jql"):
        _collect_baseline_count(store, cfg, collection)

    return len(keys)


def _count_jql(client: httpx.Client, jql: str, cfg: dict) -> int:
    """Run a JQL query and return only the total count (no issue data).

    On Atlassian Cloud the only working search endpoint is
    POST ``/rest/api/3/search/jql`` which does NOT include a ``total`` field.
    We paginate through all results requesting only the ``key`` field with a
    large page size to minimize round-trips.
    On Server/DC we use the POST v2 endpoint which returns ``total`` directly.
    """
    cloud = _is_cloud(cfg)
    if not cloud:
        body = {"jql": jql, "fields": ["key"], "maxResults": 0}
        resp = _rate_limited_request(client, "POST", "/rest/api/2/search", json=body)
        resp.raise_for_status()
        return resp.json().get("total", 0)

    count = 0
    next_token: str | None = None
    while True:
        body: dict[str, Any] = {
            "jql": jql,
            "fields": ["key"],
            "maxResults": 5000,
        }
        if next_token:
            body["nextPageToken"] = next_token

        resp = _rate_limited_request(client, "POST", "/rest/api/3/search/jql", json=body)
        resp.raise_for_status()
        data = resp.json()
        issues = data.get("issues", [])
        count += len(issues)

        if data.get("isLast", True) or not issues:
            break
        next_token = data.get("nextPageToken")
        if not next_token:
            break

    return count


def _collect_baseline_count(store: Store, cfg: dict, collection: dict) -> None:
    """Fetch the baseline bug population count and per-project counts."""
    baseline_jql = collection["baseline_jql"]
    name = collection["name"]

    client = _build_client(cfg)
    if client is None:
        return

    try:
        total = _count_jql(client, baseline_jql, cfg)
        store.save_metric("baseline_total", name, total)
        log.info("Baseline total for '%s': %d issues", name, total)

        for proj_key in collection.get("projects", []):
            proj_jql = f"project = {proj_key} AND issuetype = Bug AND created <= 2026-03-22 AND (status in (New, Backlog, Refinement, \"To Do\") OR status changed from (New, Backlog, Refinement, \"To Do\") after 2026-03-22)"
            try:
                count = _count_jql(client, proj_jql, cfg)
                store.save_metric("baseline_total", f"{name}:{proj_key}", count)
                log.info("  %s: %d issues", proj_key, count)
            except Exception as e:
                log.warning("Failed to get baseline for %s: %s", proj_key, e)
    finally:
        client.close()
