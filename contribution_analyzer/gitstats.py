"""Read per-author contribution stats out of a local git repo.

Everything is aggregate metadata: commit/line/file counts, cadence, a coarse
hour histogram, and — for team mode — commit-type / topic / path buckets plus a
few derived rates (rework ratio, tickets and feature commits per month, ...).
Subjects and paths are inspected to build the buckets, but only counts leave
this module; raw text and paths never do.

Classification matches whole-word tokens (and a few multi-word phrases), not raw
substrings — so "author" doesn't read as security, "observer" isn't infra,
"maintain" isn't AI, "latest" isn't a test, and "prefix" isn't a fix.
"""

from __future__ import annotations

import re
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone

SEP = "\x1f"
COMMIT = "\x1e__COMMIT__"
PRETTY = COMMIT + "%x1f%H%x1f%an%x1f%ae%x1f%at%x1f%P%x1f%s"

TOKEN = re.compile(r"[a-z0-9]+")
TICKET = re.compile(r"(?:#\d+|\b[A-Z][A-Z0-9]+-\d+\b)")
PREFIX = re.compile(r"^\s*([a-zA-Z]+)\s*(?:\([^)]*\))?\s*!?\s*:")

PREFIX_TYPES = {
    "feat": "feat", "feature": "feat",
    "fix": "fix", "bugfix": "fix", "hotfix": "fix",
    "docs": "docs", "doc": "docs",
    "style": "style", "refactor": "refactor", "perf": "perf",
    "test": "test", "tests": "test",
    "build": "build", "deps": "build",
    "ci": "ci", "chore": "chore", "revert": "revert",
}

# (tokens, phrases) per bucket. A subject matches if it shares a whole-word
# token with the set, or contains one of the phrases. Inflections are listed
# explicitly rather than matched by prefix, to stay precise.
def _b(tokens, *phrases):
    return (set(tokens.split()), phrases)

# Commit type — single label, first match in this order wins.
TYPES = [
    ("revert", _b("revert reverts reverted reverting")),
    ("fix", _b("fix fixes fixed fixing bug bugs bugfix hotfix patch patches "
               "patched resolve resolves resolved correct corrects corrected")),
    ("perf", _b("perf performance optimize optimise optimized optimised "
                "optimization optimisation faster latency", "speed up")),
    ("test", _b("test tests tested testing spec specs coverage")),
    ("docs", _b("readme docs doc document documentation changelog comment comments")),
    ("ci", _b("ci pipeline pipelines workflow workflows", "github action")),
    ("build", _b("build builds rebuild bump bumps bumped upgrade upgrades "
                 "upgraded dependency dependencies deps")),
    ("refactor", _b("refactor refactors refactored refactoring rename renames "
                    "renamed cleanup simplify simplified tidy", "clean up")),
    ("feat", _b("add adds added adding implement implements implemented "
                "introduce introduces introduced support supports supported "
                "create creates created new feature features")),
]

# Topics — multiple may apply to one commit.
TOPICS = [
    ("automation", _b("ai agent agents llm llms claude copilot automation "
                      "automate automated codegen scaffold bot bots")),
    ("infra", _b("infra infrastructure deploy deployed deployment docker "
                 "dockerfile kubernetes k8s terraform ansible helm nginx cron "
                 "crontab server servers serverless aws gcp azure")),
    ("ci", _b("ci pipeline pipelines workflow workflows jenkins gitlab",
              "github action")),
    ("security", _b("security secure vuln vulnerability cve auth authn authz "
                    "authenticate authentication authorize authorization oauth "
                    "sanitize sanitized csrf xss injection secret secrets "
                    "credential credentials")),
    ("tech_debt", _b("remove removes removed removal delete deletes deleted "
                     "deprecate deprecated deprecation retire retired kill kills "
                     "killed drop drops dropped unused obsolete cleanup",
                     "dead code", "tech debt", "technical debt")),
    ("planning", _b("plan plans planning planned rfc proposal proposals "
                    "investigation incident incidents postmortem spike adr adrs",
                    "design doc", "post-mortem", "implementation plan")),
    ("typing_quality", _b("mypy psalm phpcs phpstan lint linter linting eslint "
                          "tslint ruff typescript typecheck",
                          "strict_types", "type hint", "type check",
                          "static analysis")),
]

# paths that shouldn't count toward authored feature churn
GENERATED = ("package-lock.json", "yarn.lock", "pnpm-lock.yaml", "/dist/",
             "/vendor/", "node_modules/", ".min.")


class GitError(RuntimeError):
    pass


@dataclass
class ContributorStats:
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
    subject_counts: Counter = field(default_factory=Counter)  # for duplicate detection

    categories: Counter = field(default_factory=Counter)
    topics: Counter = field(default_factory=Counter)
    path_categories: Counter = field(default_factory=Counter)
    ticket_refs: set = field(default_factory=set)
    ci_infra_commits: int = 0

    md_files: set = field(default_factory=set)
    feat_insertions: int = 0
    feat_deletions: int = 0
    _cur_is_feat: bool = False

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
        self.subject_counts[subject.strip().lower()] += 1

        cat = classify_subject(subject)
        self.categories[cat] += 1
        self._cur_is_feat = cat == "feat"

        topics = classify_topics(subject)
        for t in topics:
            self.topics[t] += 1
        if {"infra", "ci"} & set(topics):
            self.ci_infra_commits += 1

        self.ticket_refs.update(TICKET.findall(subject))

    def record_file(self, added, deleted, path):
        self.files_touched.add(path)
        ext = extension(path)
        self.extensions[ext] += 1
        self.path_categories[classify_path(path)] += 1
        if ext in (".md", ".mdx"):
            self.md_files.add(path)
        if added == "-" or deleted == "-":
            self.binary_edits += 1
            return
        try:
            ins, dele = int(added), int(deleted)
        except ValueError:
            return
        self.insertions += ins
        self.deletions += dele
        if self._cur_is_feat and not is_generated(path):
            self.feat_insertions += ins
            self.feat_deletions += dele

    def to_metrics(self) -> dict:
        span_days = 0
        if self.first_commit_ts is not None and self.last_commit_ts is not None:
            span_days = max(1, round((self.last_commit_ts - self.first_commit_ts) / 86400))

        churn = self.insertions + self.deletions
        months = len(self.active_months)
        per_month = (lambda n: round(n / months, 1)) if months else (lambda n: 0)
        rework = self.categories.get("fix", 0) + self.categories.get("revert", 0)
        dups = sum(n - 1 for n in self.subject_counts.values() if n > 1)
        avg_subject = (round(sum(self.subject_lengths) / len(self.subject_lengths), 1)
                       if self.subject_lengths else 0)

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
            "commits_per_active_month": per_month(self.commits),
            "net_lines_per_active_month": per_month(self.insertions - self.deletions),
            "feature_commits_per_active_month": per_month(self.categories.get("feat", 0)),
            "net_feature_lines_per_active_month": (
                round((self.feat_insertions - self.feat_deletions) / months) if months else 0
            ),
            "avg_commit_size_lines": round(churn / self.commits, 1) if self.commits else 0,
            "avg_subject_length_chars": avg_subject,
            "short_subject_ratio": short_subject_ratio(self.subject_lengths),
            "rework_ratio": round(rework / self.commits, 3) if self.commits else 0.0,
            "duplicate_commit_ratio": round(dups / self.commits, 3) if self.commits else 0.0,
            "max_subject_repeats": max(self.subject_counts.values(), default=0),
            "distinct_tickets_referenced": len(self.ticket_refs),
            "tickets_per_active_month": per_month(len(self.ticket_refs)),
            "doc_commit_share": round(self.categories.get("docs", 0) / self.commits, 3) if self.commits else 0.0,
            "markdown_files_touched": len(self.md_files),
            "ci_infra_commits": self.ci_infra_commits,
            "commit_categories": dict(self.categories.most_common()),
            "topic_commits": dict(self.topics.most_common()),
            "path_categories": dict(self.path_categories.most_common()),
            "top_extensions": dict(self.extensions.most_common(8)),
            "hour_distribution": bucket_hours(self.hour_histogram),
            "active_hour_range": hour_range(self.hour_histogram),
            "first_commit": iso_day(self.first_commit_ts),
            "last_commit": iso_day(self.last_commit_ts),
        }


def collect(repo_path, branch=None) -> dict[str, ContributorStats]:
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
        return max(matches, key=lambda c: c.commits)
    return None


def classify_subject(subject):
    m = PREFIX.match(subject)
    if m:
        kind = PREFIX_TYPES.get(m.group(1).lower())
        if kind:
            return kind
    low = subject.lower()
    tokens = set(TOKEN.findall(low))
    for bucket, spec in TYPES:
        if matches(tokens, low, spec):
            return bucket
    return "other"


def classify_topics(subject):
    low = subject.lower()
    tokens = set(TOKEN.findall(low))
    return [name for name, spec in TOPICS if matches(tokens, low, spec)]


def matches(tokens, low, spec):
    words, phrases = spec
    return bool(words & tokens) or any(p in low for p in phrases)


def classify_path(path):
    low = path.lower()
    ext = extension(low)
    base = low.rsplit("/", 1)[-1]
    segs = set(low.split("/"))
    if (".github/workflows" in low or ".gitlab-ci" in low or "azure-pipelines" in low
            or base == "jenkinsfile" or ".circleci" in segs):
        return "ci"
    if (ext == ".tf" or base in ("dockerfile", "docker-compose.yml")
            or segs & {"k8s", "terraform", "ansible", "helm", "deploy", "infra", "charts"}):
        return "infra"
    if ext in (".md", ".mdx", ".rst", ".adoc") or base.startswith("readme") or segs & {"docs", "doc"}:
        return "docs"
    if (segs & {"test", "tests", "spec", "specs", "__tests__"}
            or ".test." in base or ".spec." in base
            or base.startswith("test_") or base.endswith("_test.go")):
        return "tests"
    if (ext in (".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".env", ".lock")
            or "config" in segs):
        return "config"
    return "src"


def is_generated(path):
    return any(g in path.lower() for g in GENERATED)


def extension(path):
    base = path.rsplit("/", 1)[-1]
    if base.startswith(".") and base.count(".") == 1:
        return base
    if "." in base:
        return "." + base.rsplit(".", 1)[-1].lower()
    return "(none)"


def bucket_hours(counter):
    total = sum(counter.values()) or 1
    out = {"00-06": 0, "06-12": 0, "12-18": 0, "18-24": 0}
    for hour, n in counter.items():
        out["%02d-%02d" % (hour // 6 * 6, hour // 6 * 6 + 6)] += n
    return {k: round(v / total, 3) for k, v in out.items()}


def hour_range(counter):
    """Tightest UTC hour window, ignoring hours with <1% of commits."""
    total = sum(counter.values())
    if not total:
        return None
    hours = sorted(h for h, n in counter.items() if n / total >= 0.01)
    if not hours:
        hours = sorted(counter)
    return "%02d:00-%02d:00" % (hours[0], hours[-1])


def short_subject_ratio(lengths, threshold=15):
    if not lengths:
        return 0.0
    return round(sum(1 for n in lengths if n < threshold) / len(lengths), 3)


def iso_day(ts):
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
