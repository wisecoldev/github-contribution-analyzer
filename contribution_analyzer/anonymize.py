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
