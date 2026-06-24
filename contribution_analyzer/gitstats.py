"""Read per-author contribution stats out of a local git repo.

Everything here is aggregate metadata: commit/line/file counts, cadence, a
coarse hour-of-day histogram, and — for team mode — locally bucketed commit
types, topics and path categories. Commit subjects and file paths are looked at
to build those buckets, but only the resulting counts are kept; the raw text
and paths never leave this module.
"""

from __future__ import annotations

import re
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone

# Separators we inject into the git pretty-format. Picked because they won't
# show up in author names or commit subjects.
SEP = "\x1f"
COMMIT = "\x1e__COMMIT__"
PRETTY = COMMIT + "%x1f%H%x1f%an%x1f%ae%x1f%at%x1f%P%x1f%s"

# "#1234", "ABC-123" (Jira) or "GH-123". We only ever keep how many distinct
# ones a person referenced, not the IDs.
TICKET = re.compile(r"(?:#\d+|\b[A-Z][A-Z0-9]+-\d+\b)")

# A conventional-commit prefix like "feat:", "fix(api)!:", "ci :".
PREFIX = re.compile(r"^\s*([a-zA-Z]+)\s*(?:\([^)]*\))?\s*!?\s*:")

PREFIX_TYPES = {
    "feat": "feat", "feature": "feat",
    "fix": "fix", "bugfix": "fix", "hotfix": "fix",
    "docs": "docs", "doc": "docs",
    "style": "style",
    "refactor": "refactor",
    "perf": "perf",
    "test": "test", "tests": "test",
    "build": "build", "deps": "build",
    "ci": "ci",
    "chore": "chore",
    "revert": "revert",
}

# Used when there's no conventional prefix. Order matters — first hit wins, so
# "fix the docs" lands in fix, not docs.
KEYWORD_TYPES = [
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

# Topics can overlap (one commit may be both "infra" and "ci").
TOPICS = [
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

# First matching rule wins for a given file path.
PATH_RULES = [
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
    pass


@dataclass
class ContributorStats:
    # name/email are kept locally for self-detection and --identify; the
    # anonymized payload drops them.
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

    categories: Counter = field(default_factory=Counter)
    topics: Counter = field(default_factory=Counter)
    path_categories: Counter = field(default_factory=Counter)
    ticket_refs: set = field(default_factory=set)

    def record_commit(self, ts, subject, is_merge=False):
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

        self.categories[classify_subject(subject)] += 1
        for topic in classify_topics(subject):
            self.topics[topic] += 1
        self.ticket_refs.update(TICKET.findall(subject))

    def record_file(self, added, deleted, path):
        self.files_touched.add(path)
        self.extensions[extension(path)] += 1
        self.path_categories[classify_path(path)] += 1
        if added == "-" or deleted == "-":
            self.binary_edits += 1
            return
        try:
            self.insertions += int(added)
            self.deletions += int(deleted)
        except ValueError:
            pass

    def to_metrics(self) -> dict:
        span_days = 0
        if self.first_commit_ts is not None and self.last_commit_ts is not None:
            span_days = max(1, round((self.last_commit_ts - self.first_commit_ts) / 86400))

        churn = self.insertions + self.deletions
        months = len(self.active_months) or 0
        if self.subject_lengths:
            avg_subject = round(sum(self.subject_lengths) / len(self.subject_lengths), 1)
        else:
            avg_subject = 0

        return {
            "commits": self.commits,
            "merge_commits": self.merge_commits,
            "insertions": self.insertions,
            "deletions": self.deletions,
            "net_churn": churn,
            "distinct_files_touched": len(self.files_touched),
            "binary_edits": self.binary_edits,
            "active_days": len(self.active_days),
            "active_months": months,
            "span_days": span_days,
            "commits_per_active_month": round(self.commits / months, 1) if months else 0,
            "net_lines_per_active_month": (
                round((self.insertions - self.deletions) / months, 1) if months else 0
            ),
            "avg_commit_size_lines": round(churn / self.commits, 1) if self.commits else 0,
            "avg_subject_length_chars": avg_subject,
            "short_subject_ratio": short_subject_ratio(self.subject_lengths),
            "distinct_tickets_referenced": len(self.ticket_refs),
            "commit_categories": dict(self.categories.most_common()),
            "topic_commits": dict(self.topics.most_common()),
            "path_categories": dict(self.path_categories.most_common()),
            "top_extensions": dict(self.extensions.most_common(8)),
            "hour_distribution": bucket_hours(self.hour_histogram),
            "first_commit": iso_day(self.first_commit_ts),
            "last_commit": iso_day(self.last_commit_ts),
        }


def collect(repo_path, branch=None) -> dict[str, ContributorStats]:
    """Aggregate per-author stats from `git log --numstat`."""
    args = ["git", "-C", repo_path, "log", "--no-color", "--numstat",
            "--pretty=format:" + PRETTY]
    if branch:
        args.append(branch)

    try:
        proc = subprocess.run(args, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", check=True)
    except FileNotFoundError as exc:
        raise GitError("git executable not found on PATH") from exc
    except subprocess.CalledProcessError as exc:
        raise GitError((exc.stderr or "").strip() or "git log failed") from exc

    stats: dict[str, ContributorStats] = {}
    current = None

    for raw in proc.stdout.split("\n"):
        if raw.startswith(COMMIT):
            parts = raw.split(SEP)
            if len(parts) < 7:
                current = None
                continue
            _, _hash, name, email, ts_str, parents, subject = parts[:7]
            try:
                ts = int(ts_str)
            except ValueError:
                current = None
                continue
            key = (email or name).strip().lower()
            current = stats.get(key)
            if current is None:
                current = ContributorStats(key=key, name=name, email=email)
                stats[key] = current
            current.record_commit(ts, subject, is_merge=len(parents.split()) > 1)
        elif current is not None and raw.strip():
            cols = raw.split("\t")
            if len(cols) >= 3:
                current.record_file(cols[0], cols[1], cols[2])

    return stats


def detect_self(repo_path):
    """(name, email) from git config, or (None, None)."""
    def cfg(key):
        try:
            out = subprocess.run(["git", "-C", repo_path, "config", "--get", key],
                                 capture_output=True, text=True, check=True)
            return out.stdout.strip() or None
        except (subprocess.CalledProcessError, FileNotFoundError):
            return None

    return cfg("user.name"), cfg("user.email")


def match_author(stats, query):
    q = (query or "").strip().lower()
    if not q:
        return None
    if q in stats:
        return stats[q]
    for c in stats.values():
        if c.email.lower() == q or c.name.lower() == q:
            return c
    matches = [c for c in stats.values() if q in c.name.lower() or q in c.email.lower()]
    if len(matches) == 1:
        return matches[0]
    if matches:
        # ambiguous fragment — go with whoever committed most
        return max(matches, key=lambda c: c.commits)
    return None


def classify_subject(subject):
    m = PREFIX.match(subject)
    if m:
        kind = PREFIX_TYPES.get(m.group(1).lower())
        if kind:
            return kind
    low = subject.lower()
    for bucket, needles in KEYWORD_TYPES:
        if any(n in low for n in needles):
            return bucket
    return "other"


def classify_topics(subject):
    low = subject.lower()
    return [name for name, needles in TOPICS if any(n in low for n in needles)]


def classify_path(path):
    low = path.lower()
    for bucket, needles in PATH_RULES:
        if any(n in low for n in needles):
            return bucket
    return "src"


def extension(path):
    base = path.rsplit("/", 1)[-1]
    if base.startswith(".") and base.count(".") == 1:
        return base  # dotfile, e.g. .gitignore
    if "." in base:
        return "." + base.rsplit(".", 1)[-1].lower()
    return "(none)"


def bucket_hours(counter):
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


def short_subject_ratio(lengths, threshold=15):
    if not lengths:
        return 0.0
    return round(sum(1 for n in lengths if n < threshold) / len(lengths), 3)


def iso_day(ts):
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
