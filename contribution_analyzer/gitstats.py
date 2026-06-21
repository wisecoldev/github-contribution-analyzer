"""Collect per-contributor contribution metadata from a local git repository.

Only aggregate, non-confidential metadata is collected:

- commit counts (regular and merge)
- lines added / removed (churn)
- number of distinct files touched
- file-*extension* distribution (never full paths — directory names can be
  proprietary)
- activity span and number of active days
- commit-subject *length* distribution (a proxy for how descriptive messages
  are — never the message text itself)
- hour-of-day commit distribution (a rough working-pattern signal)

No commit message bodies, no source code, and no file paths leave this module.
"""

from __future__ import annotations

import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

# A record separator unlikely to occur in author names or subjects.
_FIELD_SEP = "\x1f"
_COMMIT_MARKER = "\x1e__COMMIT__"

# git log placeholder: marker, hash, author name, author email, author unix
# timestamp, parent hashes, commit subject. %x1f is a literal field separator
# byte. Parent hashes (%P) let us count merges (2+ parents) reliably.
_PRETTY = _COMMIT_MARKER + "%x1f%H%x1f%an%x1f%ae%x1f%at%x1f%P%x1f%s"


class GitError(RuntimeError):
    """Raised when the repository cannot be read with git."""


@dataclass
class ContributorStats:
    """Aggregated, non-identifying metadata for one contributor."""

    # Canonical identity key (lowercased email) — used only locally, never sent
    # anywhere. Anonymisation happens in anonymize.py before any output.
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

    extensions: Counter = field(default_factory=Counter)
    hour_histogram: Counter = field(default_factory=Counter)
    subject_lengths: list = field(default_factory=list)

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
        self.hour_histogram[dt.hour] += 1
        self.subject_lengths.append(len(subject))

    def record_file(self, added: str, deleted: str, path: str) -> None:
        self.files_touched.add(path)
        ext = _extension(path)
        self.extensions[ext] += 1
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
            "span_days": span_days,
            "avg_commit_size_lines": (
                round(churn / self.commits, 1) if self.commits else 0
            ),
            "avg_subject_length_chars": avg_subject,
            "short_subject_ratio": _short_subject_ratio(self.subject_lengths),
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
    """Find the contributor whose name or email matches ``query``.

    Matching is case-insensitive: exact key/email/name first, then substring.
    """
    q = query.strip().lower()
    if not q:
        return None
    # Exact email/key.
    if q in stats:
        return stats[q]
    # Exact name or email.
    for c in stats.values():
        if c.email.lower() == q or c.name.lower() == q:
            return c
    # Substring fallback (e.g. just a first name or a username fragment).
    matches = [
        c for c in stats.values() if q in c.name.lower() or q in c.email.lower()
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        # Prefer the most active among ambiguous matches.
        return max(matches, key=lambda c: c.commits)
    return None


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def _extension(path: str) -> str:
    base = path.rsplit("/", 1)[-1]
    if base.startswith(".") and base.count(".") == 1:
        # Dotfile such as .gitignore — treat the whole name as the "ext".
        return base
    if "." in base:
        return "." + base.rsplit(".", 1)[-1].lower()
    return "(none)"


def _top_extensions(counter: Counter, limit: int = 8) -> dict:
    return {ext: n for ext, n in counter.most_common(limit)}


def _normalise_hours(counter: Counter) -> dict:
    total = sum(counter.values()) or 1
    # Bucket into four 6-hour windows for a coarse, privacy-safe rhythm signal.
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
