"""Minimal Claude Messages API client built on the Python standard library.

Uses ``urllib`` (no third-party dependencies) to call the streaming Messages
API. Defaults to ``claude-opus-4-8`` with adaptive thinking, and streams the
response via Server-Sent Events so long reports don't hit request timeouts.

Reference: POST https://api.anthropic.com/v1/messages
Headers: x-api-key, anthropic-version: 2023-06-01
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-opus-4-8"


class ClaudeError(RuntimeError):
    """Raised when the Claude API call fails."""


def generate_report(
    payload: dict,
    *,
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
    max_tokens: int = 4096,
    effort: str = "high",
    timeout: float = 600.0,
) -> str:
    """Send the anonymized payload to Claude and return the report text.

    ``payload`` is the output of :func:`anonymize.build_payload` — it contains
    no identities, no code, and no commit-message text.
    """
    return _request(
        _SYSTEM_PROMPT,
        _user_prompt(payload),
        api_key=api_key,
        model=model,
        max_tokens=max_tokens,
        effort=effort,
        timeout=timeout,
    )


def generate_team_report(
    payload: dict,
    *,
    identify: bool = False,
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
    max_tokens: int = 8192,
    effort: str = "high",
    timeout: float = 600.0,
) -> str:
    """Generate the whole-team, ranked per-contributor analysis.

    ``payload`` is the output of :func:`anonymize.build_team_payload`. It
    carries only aggregate counts/ratios — never commit text, paths, or code.
    When ``identify`` is False (the default) the labels are
    ``Contributor #1``…; when True they are real display names.
    """
    return _request(
        _TEAM_SYSTEM_PROMPT,
        _team_user_prompt(payload, identify=identify),
        api_key=api_key,
        model=model,
        max_tokens=max_tokens,
        effort=effort,
        timeout=timeout,
    )


def _request(
    system: str,
    user_content: str,
    *,
    api_key: str | None,
    model: str,
    max_tokens: int,
    effort: str,
    timeout: float,
) -> str:
    """POST a single Messages request and return the streamed text."""
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ClaudeError(
            "No API key. Set ANTHROPIC_API_KEY or pass --api-key. "
            "(Get one at https://console.anthropic.com/)"
        )

    body = {
        "model": model,
        "max_tokens": max_tokens,
        "stream": True,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": effort},
        "system": system,
        "messages": [{"role": "user", "content": user_content}],
    }

    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=data,
        method="POST",
        headers={
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return _read_sse_text(resp)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise ClaudeError(f"HTTP {exc.code} from Claude API: {detail}") from exc
    except urllib.error.URLError as exc:
        raise ClaudeError(f"Network error calling Claude API: {exc.reason}") from exc


def _read_sse_text(resp) -> str:
    """Accumulate text deltas from the streamed SSE response."""
    chunks: list[str] = []
    refusal = False
    for raw in resp:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        if not payload:
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        etype = event.get("type")
        if etype == "content_block_delta":
            delta = event.get("delta", {})
            if delta.get("type") == "text_delta":
                chunks.append(delta.get("text", ""))
        elif etype == "message_delta":
            if event.get("delta", {}).get("stop_reason") == "refusal":
                refusal = True

    text = "".join(chunks).strip()
    if refusal and not text:
        raise ClaudeError(
            "The model declined to produce a report for this input."
        )
    if not text:
        raise ClaudeError("The Claude API returned an empty response.")
    return text


_SYSTEM_PROMPT = (
    "You are an engineering-management analyst. You will be given anonymized, "
    "aggregate git contribution METADATA for one engineer (labelled 'You') and "
    "their anonymized peers (labelled 'Contributor A', 'Contributor B', ...) on "
    "a single repository. You never see source code, commit message text, file "
    "names, or real identities — only counts and ratios.\n\n"
    "Write a fair, specific, and constructive review with these sections:\n"
    "1. Summary — 2-3 sentences on the engineer's overall contribution profile.\n"
    "2. Strengths — bullet points, each grounded in a specific metric.\n"
    "3. Areas to improve — bullet points, each grounded in a specific metric, "
    "framed constructively.\n"
    "4. How you compare to peers — describe the engineer relative to the "
    "anonymized distribution (e.g. 'top third by commit volume', 'larger average "
    "commit size than most peers'). NEVER guess at or reveal any contributor's "
    "real identity, and never invent data you weren't given.\n"
    "5. Caveats — note that these are quantitative proxies (commit counts, churn, "
    "cadence), not a measure of impact or code quality.\n\n"
    "Be honest but encouraging. Use Markdown headings. Keep it under ~600 words."
)


def _user_prompt(payload: dict) -> str:
    return (
        "Here is the anonymized contribution metadata. Analyze 'You' and compare "
        "to the anonymized peers. Do not reveal or speculate about real "
        "identities, and do not reference any confidential or proprietary detail "
        "(there is none in this data).\n\n"
        "```json\n" + json.dumps(payload, indent=2) + "\n```"
    )


_TEAM_SYSTEM_PROMPT = (
    "You are an engineering-management analyst writing a higher-level, "
    "whole-codebase read on a team's git contributions. You are given "
    "anonymized-or-named, aggregate git METADATA for each contributor on one "
    "repository — counts and ratios only. You never see source code, commit "
    "message text, or file paths; the commit-type / topic / path-category "
    "fields are pre-bucketed COUNTS, not the underlying text.\n\n"
    "For EACH contributor, ranked as given (the ranking is by commit volume, an "
    "activity proxy — not a performance ranking), write a profile in EXACTLY "
    "this shape:\n\n"
    "## #<rank> — <label>\n"
    "<commits> commits | <active_months> active months | "
    "<distinct_tickets_referenced> ticket refs\n"
    "**Style** — 2-4 sentences inferring how this person works, grounded in the "
    "metrics (cadence, commit size, the commit-type and topic mix, path "
    "categories, time-of-day rhythm). Be specific and fair.\n"
    "**Strengths** — 3-6 bullets, EACH grounded in a specific metric you were "
    "given (quote the number).\n"
    "**Weaknesses** — 2-5 bullets, framed constructively, each grounded in a "
    "metric. Phrase work-pattern observations (e.g. time-of-day spread) as "
    "neutral signals, never as personal judgements about a person's worth or "
    "wellbeing.\n"
    "**Key metric** — one sentence naming the single number that best "
    "characterises this contributor.\n\n"
    "Rules:\n"
    "- NEVER invent data you were not given. If a field is zero or absent, do "
    "not speculate a value.\n"
    "- If contributors are anonymized (labels like 'Contributor #1'), do NOT "
    "guess at real identities.\n"
    "- Commit counts, churn, ticket counts, and cadence are GAMEABLE PROXIES "
    "FOR ACTIVITY, not measures of impact, seniority, or code quality. State "
    "this explicitly in a short closing 'Caveats' section, and avoid implying "
    "the #1-ranked contributor is the 'best' engineer.\n"
    "- Be honest, specific, and constructive. Markdown headings. No source "
    "code, no real ticket IDs, no file paths in your output."
)


def _team_user_prompt(payload: dict, *, identify: bool) -> str:
    mode = (
        "Contributors are identified by real display name."
        if identify
        else "Contributors are anonymized as 'Contributor #N'; do not attempt to "
        "de-anonymize them."
    )
    return (
        "Here is the whole-team, ranked contribution metadata for one "
        f"repository. {mode} Produce one profile per contributor in the exact "
        "shape described, then a short shared 'Caveats' section. Ground every "
        "claim in the numbers provided; invent nothing.\n\n"
        "```json\n" + json.dumps(payload, indent=2) + "\n```"
    )
