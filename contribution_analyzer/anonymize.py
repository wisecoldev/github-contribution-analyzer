"""Turn raw per-author stats into an output-safe payload.

For single-subject mode the person is "You" and everyone else is "Contributor
A", "Contributor B", ... ordered by commit count. For team mode every author is
ranked; labels are "Contributor #1.." unless --identify is on. Names and emails
are dropped from the anonymized payloads.
"""

from __future__ import annotations

import string

from .gitstats import ContributorStats


def build_payload(stats, subject, max_peers=12):
    """Subject metrics + anonymized peers + repo totals."""
    peers = sorted(
        (c for c in stats.values() if c.key != subject.key),
        key=lambda c: c.commits,
        reverse=True,
    )

    labelled = [
        {"label": peer_label(i), **p.to_metrics()}
        for i, p in enumerate(peers[:max_peers])
    ]

    total = sum(c.commits for c in stats.values())
    share = round(subject.commits / total, 3) if total else 0.0

    return {
        "repository_totals": {
            "total_contributors": len(stats),
            "total_commits": total,
            "peers_shown": len(labelled),
            "peers_omitted": max(0, len(peers) - len(labelled)),
        },
        "subject": {
            "label": "You",
            "commit_share_of_repo": share,
            "rank_by_commits": rank_of(stats, subject),
            **subject.to_metrics(),
        },
        "peers": labelled,
    }


def build_team_payload(stats, *, identify=False, top=None, min_commits=1):
    """Rank every contributor (by commit volume) into an aggregate payload.

    Anonymized as "Contributor #N" unless ``identify`` is set, in which case the
    real display name is used. Either way only counts/ratios are included.
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
    for i, c in enumerate(ranked):
        share = round(c.commits / total_commits, 3) if total_commits else 0.0
        contributors.append({
            "rank": i + 1,
            "label": display_name(c) if identify else f"Contributor #{i + 1}",
            "commit_share_of_repo": share,
            **c.to_metrics(),
        })

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


def display_name(c):
    return (c.name or c.email or c.key or "Unknown").strip()


def peer_label(index):
    # A, B, ... Z, AA, AB, ...
    letters = string.ascii_uppercase
    if index < len(letters):
        suffix = letters[index]
    else:
        suffix = letters[index // len(letters) - 1] + letters[index % len(letters)]
    return f"Contributor {suffix}"


def rank_of(stats, subject):
    ordered = sorted(stats.values(), key=lambda c: c.commits, reverse=True)
    for i, c in enumerate(ordered, start=1):
        if c.key == subject.key:
            return i
    return len(ordered)
