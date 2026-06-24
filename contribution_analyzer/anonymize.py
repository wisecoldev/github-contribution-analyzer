"""Turn raw per-contributor stats into an anonymized, output-safe payload.

The subject (the person being analyzed) is labelled ``"You"``. Every other
contributor is labelled ``"Contributor A"``, ``"Contributor B"``, ... in
descending order of commit count. Real names and emails are dropped entirely —
they never appear in the payload that is written to disk or sent to the Claude
API.
"""

from __future__ import annotations

import string

from .gitstats import ContributorStats


def build_payload(
    stats: dict[str, ContributorStats],
    subject: ContributorStats,
    max_peers: int = 12,
) -> dict:
    """Build the anonymized comparison payload.

    Returns a dict with ``subject`` metrics, a list of anonymized ``peers``,
    and repo-wide ``totals`` for context — no identifying fields anywhere.
    """
    peers = sorted(
        (c for c in stats.values() if c.key != subject.key),
        key=lambda c: c.commits,
        reverse=True,
    )

    labelled_peers = []
    for idx, peer in enumerate(peers[:max_peers]):
        labelled_peers.append(
            {"label": _peer_label(idx), **peer.to_metrics()}
        )

    total_commits = sum(c.commits for c in stats.values())
    total_contributors = len(stats)

    subject_metrics = subject.to_metrics()
    subject_share = (
        round(subject.commits / total_commits, 3) if total_commits else 0.0
    )

    return {
        "repository_totals": {
            "total_contributors": total_contributors,
            "total_commits": total_commits,
            "peers_shown": len(labelled_peers),
            "peers_omitted": max(0, len(peers) - len(labelled_peers)),
        },
        "subject": {
            "label": "You",
            "commit_share_of_repo": subject_share,
            "rank_by_commits": _rank(stats, subject),
            **subject_metrics,
        },
        "peers": labelled_peers,
    }


def build_team_payload(
    stats: dict[str, ContributorStats],
    *,
    identify: bool = False,
    top: int | None = None,
    min_commits: int = 1,
) -> dict:
    """Build a whole-team, ranked payload for a higher-level codebase analysis.

    Contributors are ranked by commit volume (an *activity* proxy — see the
    caveat the report always emits). By default every contributor is
    anonymized to ``Contributor #1``, ``Contributor #2``, …; pass
    ``identify=True`` to use real display names instead (opt-in, for your own
    team retrospective).

    Either way the per-contributor payload carries only aggregate counts and
    ratios — never commit-message text, file paths, or source code.
    """
    ranked = sorted(
        (c for c in stats.values() if c.commits >= min_commits),
        key=lambda c: (c.commits, c.insertions + c.deletions),
        reverse=True,
    )
    if top:
        ranked = ranked[:top]

    total_commits = sum(c.commits for c in stats.values())
    total_lines = sum(c.insertions + c.deletions for c in stats.values())

    contributors = []
    for idx, c in enumerate(ranked):
        metrics = c.to_metrics()
        share = round(c.commits / total_commits, 3) if total_commits else 0.0
        entry = {
            "rank": idx + 1,
            "label": (_display_name(c) if identify else f"Contributor #{idx + 1}"),
            "commit_share_of_repo": share,
            **metrics,
        }
        contributors.append(entry)

    return {
        "mode": "team",
        "identified": bool(identify),
        "repository_totals": {
            "total_contributors": len(stats),
            "contributors_ranked": len(contributors),
            "total_commits": total_commits,
            "total_churn_lines": total_lines,
        },
        "contributors": contributors,
    }


def _display_name(c: ContributorStats) -> str:
    return (c.name or c.email or c.key or "Unknown").strip()


def _peer_label(index: int) -> str:
    # A, B, ... Z, AA, AB, ...
    letters = string.ascii_uppercase
    if index < len(letters):
        suffix = letters[index]
    else:
        first = letters[index // len(letters) - 1]
        second = letters[index % len(letters)]
        suffix = first + second
    return f"Contributor {suffix}"


def _rank(stats: dict[str, ContributorStats], subject: ContributorStats) -> int:
    ordered = sorted(stats.values(), key=lambda c: c.commits, reverse=True)
    for i, c in enumerate(ordered, start=1):
        if c.key == subject.key:
            return i
    return len(ordered)
