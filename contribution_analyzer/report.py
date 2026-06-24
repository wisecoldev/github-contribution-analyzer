"""Build the Markdown report from the metrics payload + optional narrative."""

from __future__ import annotations

from datetime import datetime, timezone


def render(payload, narrative, *, repo_path, model):
    subject = payload["subject"]
    totals = payload["repository_totals"]
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    out = [
        "# Contribution analysis",
        "",
        f"_Generated {stamp}._",
        "",
        "> Built from **anonymized, aggregate git metadata only** — no source "
        "code, commit text, file paths, or real identities. Other contributors "
        "show up as `Contributor A`, `B`, … Commit counts and churn are "
        "activity proxies, **not** impact or code quality.",
        "",
    ]

    if narrative:
        out += [narrative.strip(), ""]
        if model:
            out += [f"_Narrative by `{model}`._", ""]

    out += [
        "---",
        "",
        "## Your metrics (raw)",
        "",
        f"- Contributors analyzed: **{totals['total_contributors']}** "
        f"({totals['total_commits']} commits total)",
        f"- Your rank by commit count: **#{subject['rank_by_commits']}** "
        f"of {totals['total_contributors']}",
        f"- Your share of all commits: **{pct(subject['commit_share_of_repo'])}**",
        "",
        metric_table(subject),
        "",
        "## Anonymized peer comparison",
        "",
        comparison_table(payload),
        "",
    ]
    if totals["peers_omitted"]:
        out += [f"_{totals['peers_omitted']} more contributor(s) omitted for "
                "brevity (still counted in totals)._", ""]
    out += ["---", "", f"_Source: `{repo_path}` (local history)._", ""]
    return "\n".join(out)


def render_team(payload, narrative, *, repo_path, model):
    totals = payload["repository_totals"]
    identified = payload.get("identified", False)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    who = ("Contributors are shown by name (`--identify`)." if identified
           else "Contributors are anonymized (`Contributor #1`, `#2`, …).")

    out = [
        "# Codebase contribution analysis",
        "",
        f"_Generated {stamp}._",
        "",
        "> Aggregate git metadata only — no source code, commit text, or file "
        f"paths leave the repo (just bucketed counts). {who} Ranking is by "
        "**commit volume, an activity proxy** — these numbers are gameable and "
        "are **not** a measure of impact, seniority, or quality. A conversation "
        "starter, not a stack-rank.",
        "",
        f"**{totals['contributors_ranked']}** of **{totals['total_contributors']}** "
        f"contributors ranked · {totals['total_commits']} commits total.",
        "",
    ]

    if narrative:
        out += [narrative.strip(), ""]
        if model:
            out += [f"_Narrative by `{model}`._", ""]
    else:
        # no API key: fall back to a plain per-person metric breakdown
        for c in payload["contributors"]:
            out += [member_block(c), ""]

    out += [
        "---",
        "",
        "## Ranked metrics (raw)",
        "",
        team_table(payload),
        "",
        "---",
        "",
        f"_Source: `{repo_path}` (local history)._",
        "",
    ]
    return "\n".join(out)


def team_table(payload):
    rows = [
        "| # | Contributor | Commits | Active mo. | Net churn | Tickets | Avg size | Top focus |",
        "| ---: | --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for c in payload["contributors"]:
        rows.append(
            f"| {c['rank']} | {c['label']} | {c['commits']} | {c['active_months']} "
            f"| {c['net_churn']} | {c['distinct_tickets_referenced']} "
            f"| {c['avg_commit_size_lines']} | {top_focus(c)} |"
        )
    return "\n".join(rows)


def member_block(c):
    return "\n".join([
        f"## #{c['rank']} — {c['label']}",
        f"{c['commits']} commits | {c['active_months']} active months | "
        f"{c['distinct_tickets_referenced']} ticket refs | {c['net_churn']} net churn",
        "",
        f"- Commit-type mix: {counts(c['commit_categories'])}",
        f"- Topic focus: {counts(c['topic_commits'])}",
        f"- Where they work: {counts(c['path_categories'])}",
        f"- Cadence: {c['commits_per_active_month']} commits/active-month, "
        f"avg commit {c['avg_commit_size_lines']} lines",
        f"- Commit-time rhythm (UTC): {hours(c['hour_distribution'])}",
    ])


def top_focus(c):
    for field in ("topic_commits", "commit_categories"):
        d = c.get(field) or {}
        if d:
            name, n = next(iter(d.items()))
            return f"{name} ({n})"
    return "—"


def counts(d, limit=5):
    if not d:
        return "—"
    return ", ".join(f"{k} ({v})" for k, v in list(d.items())[:limit])


def metric_table(m):
    rows = [
        ("Commits", m["commits"]),
        ("Merge commits", m["merge_commits"]),
        ("Lines added", m["insertions"]),
        ("Lines removed", m["deletions"]),
        ("Net churn", m["net_churn"]),
        ("Avg commit size (lines)", m["avg_commit_size_lines"]),
        ("Distinct files touched", m["distinct_files_touched"]),
        ("Active days", m["active_days"]),
        ("Activity span (days)", m["span_days"]),
        ("Avg subject length (chars)", m["avg_subject_length_chars"]),
        ("Short-subject ratio", pct(m["short_subject_ratio"])),
        ("First commit", m.get("first_commit") or "—"),
        ("Last commit", m.get("last_commit") or "—"),
    ]
    table = ["| Metric | Value |", "| --- | --- |"]
    table += [f"| {label} | {value} |" for label, value in rows]
    table += [
        "",
        f"- Top file types: {ext_list(m['top_extensions'])}",
        f"- Commit-time rhythm (UTC): {hours(m['hour_distribution'])}",
    ]
    return "\n".join(table)


def comparison_table(payload):
    rows = [
        "| Contributor | Commits | Net churn | Avg commit size | Files | Active days |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
        comparison_row("**You**", payload["subject"]),
    ]
    rows += [comparison_row(p["label"], p) for p in payload["peers"]]
    return "\n".join(rows)


def comparison_row(label, m):
    return (f"| {label} | {m['commits']} | {m['net_churn']} | "
            f"{m['avg_commit_size_lines']} | {m['distinct_files_touched']} | "
            f"{m['active_days']} |")


def ext_list(ext):
    return ", ".join(f"`{k}` ({v})" for k, v in ext.items()) if ext else "—"


def hours(h):
    return ", ".join(f"{k}: {pct(v)}" for k, v in h.items())


def pct(value):
    return f"{round(value * 100)}%"
