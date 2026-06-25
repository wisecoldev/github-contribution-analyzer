"""Tiny Claude Messages API client (stdlib urllib, SSE streaming)."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-opus-4-8"


class ClaudeError(RuntimeError):
    pass


def generate_report(payload, *, api_key=None, model=DEFAULT_MODEL,
                    max_tokens=4096, effort="high", timeout=600.0):
    """Single-subject strengths/weaknesses/peer-comparison narrative."""
    return _request(SYSTEM_PROMPT, _user_prompt(payload), api_key=api_key,
                    model=model, max_tokens=max_tokens, effort=effort,
                    timeout=timeout)


def generate_team_report(payload, *, identify=False, api_key=None,
                         model=DEFAULT_MODEL, max_tokens=8192, effort="high",
                         timeout=600.0):
    """Whole-team ranked profiles. Payload is aggregate counts only."""
    return _request(TEAM_PROMPT, _team_user_prompt(payload, identify),
                    api_key=api_key, model=model, max_tokens=max_tokens,
                    effort=effort, timeout=timeout)


def _request(system, user, *, api_key, model, max_tokens, effort, timeout):
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ClaudeError("No API key. Set ANTHROPIC_API_KEY or pass --api-key.")

    body = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "stream": True,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": effort},
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }).encode("utf-8")

    req = urllib.request.Request(API_URL, data=body, method="POST", headers={
        "content-type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
    })

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return _read_stream(resp)
    except urllib.error.HTTPError as exc:
        raise ClaudeError(f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')}") from exc
    except urllib.error.URLError as exc:
        raise ClaudeError(f"network error: {exc.reason}") from exc


def _read_stream(resp):
    """Pull the text deltas out of the SSE stream."""
    out = []
    refused = False
    for raw in resp:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data:
            continue
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "content_block_delta":
            delta = event.get("delta", {})
            if delta.get("type") == "text_delta":
                out.append(delta.get("text", ""))
        elif event.get("type") == "message_delta":
            if event.get("delta", {}).get("stop_reason") == "refusal":
                refused = True

    text = "".join(out).strip()
    if refused and not text:
        raise ClaudeError("the model declined to produce a report for this input")
    if not text:
        raise ClaudeError("empty response from the API")
    return text


SYSTEM_PROMPT = (
    "You are an engineering-management analyst. You get anonymized, aggregate "
    "git contribution metadata for one engineer ('You') and anonymized peers "
    "('Contributor A', 'B', ...) on a single repo — counts and ratios only, no "
    "code, commit text, file names, or real identities.\n\n"
    "Write a fair, specific, constructive review:\n"
    "1. Summary — 2-3 sentences.\n"
    "2. Strengths — bullets, each tied to a metric.\n"
    "3. Areas to improve — bullets, each tied to a metric, framed kindly.\n"
    "4. How you compare to peers — relative to the anonymized distribution; "
    "never guess identities or invent data.\n"
    "5. Caveats — these are activity proxies, not impact or code quality.\n\n"
    "Honest but encouraging. Markdown headings. Under ~600 words."
)


def _user_prompt(payload):
    return (
        "Anonymized contribution metadata below. Analyze 'You' against the "
        "peers; don't reveal or guess identities, and there is no confidential "
        "data here to reference.\n\n```json\n"
        + json.dumps(payload, indent=2) + "\n```"
    )


TEAM_PROMPT = (
    "You are an engineering-management analyst writing a whole-codebase read on "
    "a team's git contributions. You get aggregate metadata per contributor — "
    "counts and ratios only. No source code, commit text, or file paths; the "
    "commit-type / topic / path-category fields are pre-bucketed counts.\n\n"
    "For each contributor, in the given order (ranked by commit volume, which "
    "is an activity proxy, not a performance ranking), write:\n\n"
    "## #<rank> — <label>\n"
    "<commits> commits | <active_months> active months | "
    "<distinct_tickets_referenced> tickets shipped\n"
    "**Style** — 2-4 sentences on how they work, grounded in the metrics "
    "(cadence, commit size, type/topic mix, path categories, time-of-day).\n"
    "**Strengths** — 3-6 bullets, each quoting a specific number.\n"
    "**Weaknesses** — 2-5 bullets, constructive, each tied to a metric. Treat "
    "time-of-day spread as a neutral signal, never a judgement about someone's "
    "hours or wellbeing.\n"
    "**KeyMetric** — exactly one sentence: '<tickets_per_active_month> tickets "
    "shipped per month with a <rework_ratio as %> rework ratio.'\n\n"
    "Metrics you may quote (use these exact JSON fields, invent no others):\n"
    "- topic_commits.automation = AI/agent/pipeline commits\n"
    "- topic_commits.planning = implementation-plan commits\n"
    "- topic_commits.tech_debt = removal/deprecation commits\n"
    "- ci_infra_commits = CI/deploy/infra commits (already deduped — use this, "
    "not topic_commits.infra + topic_commits.ci, which overlap)\n"
    "- topic_commits.typing_quality = static-analysis / lint commits\n"
    "- topic_commits.security = security-related commits\n"
    "- commit_categories = commit-type mix; doc_commit_share, markdown_files_touched\n"
    "- feature_commits_per_active_month, net_feature_lines_per_active_month\n"
    "- rework_ratio (+ rework_band = low/mid/high vs the team), "
    "duplicate_commit_ratio, active_hour_range\n\n"
    "Rules: never invent missing data; only call rework 'low'/'high' if "
    "rework_band says so; don't de-anonymize 'Contributor #N' labels; remember "
    "commit/churn/ticket counts are gameable activity proxies, not impact, "
    "seniority, or quality — say so in a short closing 'Caveats', and don't "
    "imply #1 is the best engineer. No code, real ticket IDs, or file paths."
)


def _team_user_prompt(payload, identify):
    mode = ("Contributors are identified by real display name."
            if identify else
            "Contributors are anonymized as 'Contributor #N' — don't de-anonymize them.")
    return (
        f"Whole-team contribution metadata for one repo. {mode} One profile per "
        "contributor in the exact shape, then a short shared 'Caveats'. Ground "
        "every claim in the numbers; invent nothing.\n\n```json\n"
        + json.dumps(payload, indent=2) + "\n```"
    )
