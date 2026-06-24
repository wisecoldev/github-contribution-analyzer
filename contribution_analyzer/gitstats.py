"""Collect per-contributor contribution metadata from a local git repository.

Only aggregate, non-confidential metadata is collected, and — importantly —
the richer classification added for whole-team ("--team") analysis is computed
*locally*: commit subjects and file paths are inspected on your machine to
bucket commits (feat / fix / docs / ci / …) and paths (ci / infra / docs /
tests / …), but only the resulting **counts** are ever exported. No raw commit
message text and no raw file paths leave this module.

What is collected per contributor:

- commit counts (regular and merge)
- lines added / removed (churn)
- number of distinct files touched
- file-*extension* distribution (never full paths)
- commit-type buckets (from the subject's conventional-commit prefix /
  keywords — counts only, never the text)
- topical buckets (infra, security, automation, planning, … — counts only)
- path-category buckets (ci / infra / docs / tests / config / src — counts
  only, derived locally from paths that never leave this module)
- count of distinct ticket references in subjects (the *count*, not the IDs)
- activity span, active days, and active months
- commit-subject *length* distribution (a proxy for how descriptive messages
  are — never the message text itself)
- hour-of-day commit distribution (a rough working-pattern signal)
"""

from __future__ import annotations

import re
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone

# A record separator unlikely to occur in author names or subjects.
_FIELD_SEP = "\x1f"
_COMMIT_MARKER = "\x1e__COMMIT__"

# git log placeholder: marker, hash, author name, author email, author unix
# timestamp, parent hashes, commit subject. %x1f is a literal field separator
# byte. Parent hashes (%P) let us count merges (2+ parents) reliably.
_PRETTY = _COMMIT_MARKER + "%x1f%H%x1f%an%x1f%ae%x1f%at%x1f%P%x1f%s"

# Ticket references in a subject: "#1234", "ABC-123" (Jira-style), "GH-123".
# We export only the *count* of distinct references, never the IDs themselves.
_TICKET_RE = re.compile(r"(?:#\d+|\b[A-Z][A-Z0-9]+-\d+\b)")

# Conventional-commit prefix, e.g. "feat:", "fix(scope)!:", "ci :".
_CONVENTIONAL_RE = re.compile(r"^\s*([a-zA-Z]+)\s*(?:\([^)]*\))?\s*!?\s*:")

_CONVENTIONAL_TYPES = {
    "feat": "feat",
    "feature": "feat",
    "fix": "fix",
    "bugfix": "fix",
    "hotfix": "fix",
    "docs": "docs",
    "doc": "docs",
    "style": "style",
    "refactor": "refactor",
    "perf": "perf",
    "test": "test",
    "tests": "test",
    "build": "build",
    "ci": "ci",
    "chore": "chore",
    "revert": "revert",
    "deps": "build",
}

# Keyword fallback when there is no conventional prefix. First match wins; the
# order encodes priority (a "fix the broken docs" commit counts as fix).
_KEYWORD_TYPES: list[tuple[str, tuple[str, ...]]] = [
    ("revert", ("revert",)),
    ("fix", ("fix", "bug", "hotfix", "patch", "resolve", "correct")),
    ("perf", ("perf", "optimi", "speed up", "faster", "latency")),
    ("test", ("test", "spec", "coverage")),
    ("docs", ("readme", "document", "docs", "changelog", "comment")),
    ("ci", ("ci ", "pipeline", "workflow", "github action", "gitlab")),
    ("build", ("bump", "upgrade", "dependency", "dependencies", "deps", "build")),
    ("refactor", ("refactor", "rename", "cleanup", "clean up", "simplify", "tidy")),
    ("feat", ("add", "implement", "introduce", "support", "create", "new ", "feature")),
]

# Topical buckets are NOT mutually exclusive — one commit can hit several.
# Each is (bucket_name, substrings). Matched against the lowercased subject.
_TOPIC_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("automation", ("ai", "agent", "claude", "llm", "copilot", "automation",
                    "pipeline", "codegen", "scaffold")),
    ("infra", ("infra", "deploy", "docker", "kubernetes", "k8s", "terraform",
               "ansible", "helm", "nginx", "cron", "server", "hetzner", "aws",
               "gcp", "azure")),
    ("ci", ("ci ", "ci/", "pipeline", "workflow", "github action", "gitlab",
            "jenkins", "build system")),
    ("security", ("security", "vuln", "cve", "auth", "sanitiz", "csrf", "xss",
                  "injection", "secret", "credential", "timing-safe", "hash_equals")),
    ("tech_debt", ("remove", "delete", "deprecat", "drop ", "retire", "kill",
                   "dead code", "unused", "cleanup", "clean up")),
    ("planning", ("plan", "rfc", "design doc", "proposal", "investigation",
                  "incident", "postmortem", "post-mortem", "spike", "adr")),
    ("typing_quality", ("strict_types", "type hint", "mypy", "psalm", "phpcs",
                        "typescript", "type-check", "typecheck", "lint")),
]

# Path categories, checked in order; first match wins per file.
_PATH_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("ci", (".github/workflows", ".gitlab-ci", "azure-pipelines", "jenkinsfile",
            ".circleci", "/ci/", "ci/")),
    ("infra", ("dockerfile", "docker-compose", "/k8s/", "kubernetes", ".tf",
               "terraform", "ansible", "helm", "/deploy", "/infra", "/charts/")),
    ("docs", ("/docs/", "docs/", "readme", ".md", ".rst", ".adoc", "/doc/")),
    ("tests", ("/test", "tests/", "/spec", "__tests__", ".test.", ".spec.",
               "_test.", "test_")),
    ("config", (".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".env",
                ".lock", "config/")),
]


class GitError(RuntimeError):
    """Raised when the repository cannot be read with git."""


@dataclass
class ContributorStats:
    """Aggregated, non-identifying metadata for one contributor.

    Real name/email are kept only locally for self-detection and for the
    optional ``--identify`` mode; anonymization (the default) drops them in
    ``anonymize.py`` before any output or API call.
    """

    key: str
    name: str
    email: str

    commits: int = 0
    merge_commits: int = 0
    insertions: int = 0
    deletions: int = 0
    files_touched: set = field(default_factory=set)
    binary_edits: int = 0

    first_commit_ts: int | None = None
    last_commit_ts: int | None = None
    active_days: set = field(default_factory=set)
    active_months: set = field(default_factory=set)

    extensions: Counter = field(default_factory=Counter)
    hour_histogram: Counter = field(default_factory=Counter)
    subject_lengths: list = field(default_factory=list)

    # Richer, locally-computed classification (counts are exported; the
    # underlying text/paths are not).
    categories: Counter = field(default_factory=Counter)        # one per commit
    topics: Counter = field(default_factory=Counter)            # 0+ per commit
    path_categories: Counter = field(default_factory=Counter)   # per file
    ticket_refs: set = field(default_factory=set)               # distinct IDs

    def record_commit(self, ts: int, subject: str, is_merge: bool = False) -> None:
        self.commits += 1
        if is_merge:
            self.merge_commits += 1
        if self.first_commit_ts is None or ts < self.first_commit_ts:
            self.first_commit_ts = ts
        if self.last_commit_ts is None or ts > self.last_commit_ts:
            self.last_commit_ts = ts
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        self.active_days.add(dt.date().isoformat())
        self.active_months.add(dt.strftime("%Y-%m"))
        self.hour_histogram[dt.hour] += 1
        self.subject_lengths.append(len(subject))

        # Local classification — only the resulting counts are exported.
        self.categories[_classify_subject(subject)] += 1
        for topic in _classify_topics(subject):
            self.topics[topic] += 1
        for ref in _TICKET_RE.findall(subject):
            self.ticket_refs.add(ref)

    def record_file(self, added: str, deleted: str, path: str) -> None:
        self.files_touched.add(path)
        ext = _extension(path)
        self.extensions[ext] += 1
        self.path_categories[_classify_path(path)] += 1
        if added == "-" or deleted == "-":
            self.binary_edits += 1
            return
        try:
            self.insertions += int(added)
            self.deletions += int(deleted)
        except ValueError:
            pass

    # --- derived, output-safe metrics ------------------------------------

    def to_metrics(self) -> dict:
        """Return a JSON-serialisable dict of non-identifying metrics."""
        span_days = 0
        if self.first_commit_ts is not None and self.last_commit_ts is not None:
            span_days = max(
                1, round((self.last_commit_ts - self.first_commit_ts) / 86400)
            )
        churn = self.insertions + self.deletions
        active_months = len(self.active_months)
        avg_subject = (
            round(sum(self.subject_lengths) / len(self.subject_lengths), 1)
            if self.subject_lengths
            else 0
        )
        return {
            "commits": self.commits,
            "merge_commits": self.merge_commits,
            "insertions": self.insertions,
            "deletions": self.deletions,
            "net_churn": churn,
            "distinct_files_touched": len(self.files_touched),
            "binary_edits": self.binary_edits,
            "active_days": len(self.active_days),
            "active_months": active_months,
            "span_days": span_days,
            "commits_per_active_month": (
                round(self.commits / active_months, 1) if active_months else 0
            ),
            "net_lines_per_active_month": (
                round((self.insertions - self.deletions) / active_months, 1)
                if active_months
                else 0
            ),
            "avg_commit_size_lines": (
                round(churn / self.commits, 1) if self.commits else 0
            ),
            "avg_subject_length_chars": avg_subject,
            "short_subject_ratio": _short_subject_ratio(self.subject_lengths),
            "distinct_tickets_referenced": len(self.ticket_refs),
            "commit_categories": dict(self.categories.most_common()),
            "topic_commits": dict(self.topics.most_common()),
            "path_categories": dict(self.path_categories.most_common()),
            "top_extensions": _top_extensions(self.extensions),
            "hour_distribution": _normalise_hours(self.hour_histogram),
            "first_commit": _iso_day(self.first_commit_ts),
            "last_commit": _iso_day(self.last_commit_ts),
        }


def collect(repo_path: str, branch: str | None = None) -> dict[str, ContributorStats]:
    """Walk the repository history and aggregate per-contributor stats.

    Returns a mapping of canonical-key -> ContributorStats.
    """
    args = [
        "git",
        "-C",
        repo_path,
        "log",
        "--no-color",
        "--numstat",
        "--pretty=format:" + _PRETTY,
    ]
    if branch:
        args.append(branch)

    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
    except FileNotFoundError as exc:  # git not installed
        raise GitError("git executable not found on PATH") from exc
    except subprocess.CalledProcessError as exc:
        msg = (exc.stderr or "").strip() or "git log failed"
        raise GitError(msg) from exc

    stats: dict[str, ContributorStats] = {}
    current: ContributorStats | None = None

    for raw in proc.stdout.split("\n"):
        if raw.startswith(_COMMIT_MARKER):
            parts = raw.split(_FIELD_SEP)
            # [marker, hash, name, email, ts, parents, subject]
            if len(parts) < 7:
                current = None
                continue
            _, _hash, name, email, ts_str, parents, subject = parts[:7]
            try:
                ts = int(ts_str)
            except ValueError:
                current = None
                continue
            is_merge = len(parents.split()) > 1
            key = (email or name).strip().lower()
            current = stats.get(key)
            if current is None:
                current = ContributorStats(key=key, name=name, email=email)
                stats[key] = current
            current.record_commit(ts, subject, is_merge=is_merge)
        elif current is not None and raw.strip():
            cols = raw.split("\t")
            if len(cols) >= 3:
                added, deleted, path = cols[0], cols[1], cols[2]
                current.record_file(added, deleted, path)

    return stats


def detect_self(repo_path: str) -> tuple[str | None, str | None]:
    """Return (name, email) from the repo/global git config, if available."""

    def _cfg(key: str) -> str | None:
        try:
            out = subprocess.run(
                ["git", "-C", repo_path, "config", "--get", key],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            return out or None
        except (subprocess.CalledProcessError, FileNotFoundError):
            return None

    return _cfg("user.name"), _cfg("user.email")


def match_author(
    stats: dict[str, ContributorStats], query: str
) -> ContributorStats | None:
    """Find the contributor whose name or email matches ``query``."""
    q = query.strip().lower()
    if not q:
        return None
    if q in stats:
        return stats[q]
    for c in stats.values():
        if c.email.lower() == q or c.name.lower() == q:
            return c
    matches = [
        c for c in stats.values() if q in c.name.lower() or q in c.email.lower()
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        return max(matches, key=lambda c: c.commits)
    return None


# --------------------------------------------------------------------------
# Local classification helpers (text/paths never leave the module — only the
# bucket the classifier returns is ever aggregated and exported).
# --------------------------------------------------------------------------


def _classify_subject(subject: str) -> str:
    """Map a commit subject to a single commit-type bucket."""
    m = _CONVENTIONAL_RE.match(subject)
    if m:
        kind = _CONVENTIONAL_TYPES.get(m.group(1).lower())
        if kind:
            return kind
    low = subject.lower()
    for bucket, needles in _KEYWORD_TYPES:
        if any(n in low for n in needles):
            return bucket
    return "other"


def _classify_topics(subject: str) -> list[str]:
    """Return the (possibly empty) list of topical buckets a subject hits."""
    low = subject.lower()
    return [name for name, needles in _TOPIC_KEYWORDS if any(n in low for n in needles)]


def _classify_path(path: str) -> str:
    low = path.lower()
    for bucket, needles in _PATH_RULES:
        if any(n in low for n in needles):
            return bucket
    return "src"


def _extension(path: str) -> str:
    base = path.rsplit("/", 1)[-1]
    if base.startswith(".") and base.count(".") == 1:
        return base
    if "." in base:
        return "." + base.rsplit(".", 1)[-1].lower()
    return "(none)"


def _top_extensions(counter: Counter, limit: int = 8) -> dict:
    return {ext: n for ext, n in counter.most_common(limit)}


def _normalise_hours(counter: Counter) -> dict:
    total = sum(counter.values()) or 1
    buckets = {"00-06": 0, "06-12": 0, "12-18": 0, "18-24": 0}
    for hour, n in counter.items():
        if hour < 6:
            buckets["00-06"] += n
        elif hour < 12:
            buckets["06-12"] += n
        elif hour < 18:
            buckets["12-18"] += n
        else:
            buckets["18-24"] += n
    return {k: round(v / total, 3) for k, v in buckets.items()}


def _short_subject_ratio(lengths: list, threshold: int = 15) -> float:
    if not lengths:
        return 0.0
    short = sum(1 for n in lengths if n < threshold)
    return round(short / len(lengths), 3)


def _iso_day(ts: int | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
