"""Parse Go test failure messages to extract structured assertions.

Raw failure messages from Gomega, testify, or plain Go t.Fatal() calls are
often very long — embedding full Kubernetes resource JSON — and the actual
root cause is buried deep in the output.  This module extracts the
meaningful parts: what failed, expected vs actual, and the root cause error.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class ParsedAssertion:
    """Structured representation of a Go test assertion failure."""

    summary: str
    timeout_seconds: float | None = None
    source_file: str | None = None
    source_line: int | None = None
    expected: str | None = None
    actual_snippet: str | None = None
    root_cause: str | None = None
    context: str | None = None
    raw_length: int = 0

    def oneliner(self) -> str:
        """Single-line summary for table cells and short displays."""
        parts = []
        if self.timeout_seconds:
            parts.append(f"timeout {self.timeout_seconds:.0f}s")
        if self.context:
            parts.append(self.context)
        if self.root_cause:
            parts.append(self.root_cause)
        elif self.expected and self.actual_snippet:
            parts.append(f"expected {self.expected}, got {self.actual_snippet}")
        elif self.expected:
            parts.append(f"expected: {self.expected}")
        return " — ".join(parts) if parts else self.summary


# ---------------------------------------------------------------------------
# Regex patterns for common Go test assertion formats
# ---------------------------------------------------------------------------

_TIMEOUT_RE = re.compile(
    r"Timed out after ([\d.]+)s", re.IGNORECASE
)
_EVENTUALLY_CONTEXT_RE = re.compile(
    r"The function passed to (?:Eventually|Consistently) failed at (.+?):(\d+) with:\s*\n\s*(.+)",
    re.MULTILINE,
)
_GOMEGA_EXPECTED_RE = re.compile(
    r"to (match expression|equal|satisfy|be true|be false|succeed|have occurred|"
    r"not have occurred|be nil|be empty|contain|have key|have len|be numerically)"
    r"(?:\s*\n\s*(?:<[^>]+>:\s*)?(.+))?",
    re.MULTILINE | re.IGNORECASE,
)
_SOURCE_FILE_RE = re.compile(
    r"(\w[\w/.-]*_test\.go):(\d+)"
)
_ASSERT_NOT_EQUAL_RE = re.compile(
    r"Not equal:\s*\n\s*expected:\s*(.+?)\s*\n\s*actual\s*:\s*(.+)",
    re.MULTILINE,
)
_ASSERT_ERROR_RE = re.compile(
    r"Error:\s*Received unexpected error:\s*\n\s*(.+)",
    re.MULTILINE,
)
_K8S_CONDITION_ERROR_RE = re.compile(
    r"message:((?:must build|error|failed|cannot|no such|timeout|context deadline|"
    r"connection refused|i/o timeout|OOMKilled|CrashLoopBackOff|"
    r"ImagePullBackOff|ErrImagePull|not found|forbidden|unauthorized|"
    r"admission webhook|evalsymlink failure|lstat)[^]\n]{0,300})",
    re.IGNORECASE,
)
_STANDALONE_ERROR_RE = re.compile(
    r"(?:context deadline exceeded|connection refused|i/o timeout|"
    r"OOMKilled|CrashLoopBackOff|ImagePullBackOff|ErrImagePull|"
    r"no such file or directory|permission denied)",
    re.IGNORECASE,
)
_GOMEGA_CONTEXT_LINE_RE = re.compile(
    r"(?:Failed to |Expected |Unexpected |Could not )(.{10,200})",
    re.IGNORECASE,
)
_PANIC_TIMEOUT_RE = re.compile(
    r"panic: test timed out after ([\d.]+[smh]+)"
)


def parse_failure_message(raw: str) -> ParsedAssertion:
    """Extract structured assertion info from a raw Go test failure message.

    Handles:
    - Gomega Eventually/Consistently timeouts with embedded K8s resource JSON
    - Gomega Expect().To() assertions
    - testify assert/require assertions
    - Plain t.Fatal() / t.Error() messages
    - Kubernetes condition errors embedded in resource dumps
    """
    if not raw:
        return ParsedAssertion(summary="(empty)", raw_length=0)

    result = ParsedAssertion(
        summary=_first_meaningful_line(raw),
        raw_length=len(raw),
    )

    # Source file and line
    m = _SOURCE_FILE_RE.search(raw)
    if m:
        result.source_file = m.group(1)
        result.source_line = int(m.group(2))

    # Timeout detection
    m = _TIMEOUT_RE.search(raw)
    if m:
        result.timeout_seconds = float(m.group(1))

    m = _PANIC_TIMEOUT_RE.search(raw)
    if m and not result.timeout_seconds:
        dur_str = m.group(1)
        result.timeout_seconds = _parse_go_duration(dur_str)

    # Gomega Eventually context ("The function passed to Eventually failed at...")
    m = _EVENTUALLY_CONTEXT_RE.search(raw)
    if m:
        result.source_file = result.source_file or m.group(1).rsplit("/", 1)[-1]
        result.source_line = result.source_line or int(m.group(2))
        result.context = m.group(3).strip()[:200]

    # If no context from Eventually, look for "Failed to..." or similar
    if not result.context:
        m = _GOMEGA_CONTEXT_LINE_RE.search(raw)
        if m:
            result.context = m.group(0).strip()[:200]

    # Expected expression/value (Gomega "to match expression", "to equal", etc.)
    m = _GOMEGA_EXPECTED_RE.search(raw)
    if m:
        matcher = m.group(1).strip()
        value = (m.group(2) or "").strip()
        if value:
            result.expected = value[:200]
        else:
            result.expected = matcher[:200]

    # testify "Not equal" pattern
    m = _ASSERT_NOT_EQUAL_RE.search(raw)
    if m:
        result.expected = m.group(1).strip()[:150]
        result.actual_snippet = m.group(2).strip()[:150]

    # testify "Error: Received unexpected error" pattern
    m = _ASSERT_ERROR_RE.search(raw)
    if m:
        result.root_cause = m.group(1).strip()[:300]

    # Kubernetes condition error messages (the real root cause)
    if not result.root_cause:
        k8s_errors = _K8S_CONDITION_ERROR_RE.findall(raw)
        if k8s_errors:
            seen = set()
            unique = []
            for e in k8s_errors:
                key = e[:80].lower()
                if key not in seen:
                    seen.add(key)
                    unique.append(e.strip())
            cause = unique[0][:300]
            # Strip trailing K8s condition metadata fields
            cause = re.sub(
                r"(?:\s+(?:reason|status|type):\S+)+\s*$", "", cause
            ).strip()
            result.root_cause = cause

    # Standalone error patterns (not in K8s conditions)
    if not result.root_cause:
        m = _STANDALONE_ERROR_RE.search(raw)
        if m:
            result.root_cause = m.group(0).strip()

    # Build a better summary from parsed fields
    result.summary = _build_summary(result)

    return result


_FRAMEWORK_LINE_RE = re.compile(
    r"^(=== (RUN|PAUSE|CONT)|--- (FAIL|PASS)|FAIL\s*$|PASS\s*$|"
    r"exit status \d|goroutine \d|"
    r"testing\.go:\d+: test executed panic|"
    r"panic: test executed panic|"
    r"\[from (parent|child) test\])",
    re.IGNORECASE,
)


def _first_meaningful_line(raw: str) -> str:
    """Get the first non-empty, non-framework line from the message."""
    for line in raw.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if _FRAMEWORK_LINE_RE.match(stripped):
            continue
        return stripped[:200]
    return raw[:200]


def _build_summary(p: ParsedAssertion) -> str:
    """Build a human-readable summary from parsed fields."""
    parts = []
    if p.timeout_seconds:
        parts.append(f"Timeout after {p.timeout_seconds:.0f}s")
    if p.context:
        parts.append(p.context)
    if p.root_cause:
        parts.append(f"root cause: {p.root_cause[:150]}")
    elif p.expected:
        exp = p.expected[:100]
        if p.actual_snippet:
            parts.append(f"expected {exp}, got {p.actual_snippet[:100]}")
        else:
            parts.append(f"expected: {exp}")
    if parts:
        return " — ".join(parts)
    return p.summary


def _parse_go_duration(s: str) -> float:
    """Parse Go duration strings like '10m0s', '600s', '1h30m'."""
    total = 0.0
    for m in re.finditer(r"([\d.]+)([smh])", s):
        val = float(m.group(1))
        unit = m.group(2)
        if unit == "h":
            total += val * 3600
        elif unit == "m":
            total += val * 60
        else:
            total += val
    return total


def format_for_report(raw: str, max_chars: int = 400) -> str:
    """Format a failure message for markdown report display.

    Returns a structured summary followed by key details, rather than
    a blind truncation of the raw message.
    """
    parsed = parse_failure_message(raw)

    lines = [parsed.summary]
    if parsed.source_file:
        loc = parsed.source_file
        if parsed.source_line:
            loc += f":{parsed.source_line}"
        lines.append(f"at {loc}")

    return " | ".join(lines)


def format_for_table(raw: str, max_chars: int = 120) -> str:
    """Short format for table cells."""
    parsed = parse_failure_message(raw)
    return parsed.oneliner()[:max_chars]
