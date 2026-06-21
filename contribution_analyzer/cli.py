"""Command-line interface for the contribution analyzer."""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__, anonymize, claude, gitstats, report


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="contribution-analyzer",
        description=(
            "Analyze your contributions to a git repository — strengths, "
            "weaknesses, and an anonymized comparison to other contributors. "
            "No code, commit text, file paths, or peer identities are exposed."
        ),
    )
    p.add_argument(
        "--repo",
        default=".",
        help="Path to the local git repository (default: current directory).",
    )
    p.add_argument(
        "--author",
        default=None,
        help=(
            "Name, email, or fragment identifying the contributor to analyze. "
            "Defaults to the repo's configured git user (you)."
        ),
    )
    p.add_argument(
        "--branch",
        default=None,
        help="Restrict history to a branch/ref (default: current HEAD history).",
    )
    p.add_argument(
        "--out",
        "-o",
        default=None,
        help="Write the Markdown report to this file (default: stdout).",
    )
    p.add_argument(
        "--no-ai",
        action="store_true",
        help="Skip the Claude narrative; emit metrics only (no API key needed).",
    )
    p.add_argument(
        "--model",
        default=claude.DEFAULT_MODEL,
        help=f"Claude model id (default: {claude.DEFAULT_MODEL}).",
    )
    p.add_argument(
        "--api-key",
        default=None,
        help="Anthropic API key (else uses ANTHROPIC_API_KEY env var).",
    )
    p.add_argument(
        "--dump-payload",
        action="store_true",
        help="Print the exact anonymized JSON that would be sent to Claude, then exit.",
    )
    p.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # Markdown output uses a few non-ASCII characters (—, …). Make sure stdout
    # can render them even on a legacy Windows code page.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass

    # 1. Collect raw stats from local git history.
    try:
        stats = gitstats.collect(args.repo, branch=args.branch)
    except gitstats.GitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not stats:
        print("error: no commits found in this repository.", file=sys.stderr)
        return 2

    # 2. Resolve the subject (the person being analyzed).
    subject = _resolve_subject(stats, args)
    if subject is None:
        return 2

    # 3. Anonymize into an output-safe payload.
    payload = anonymize.build_payload(stats, subject)

    if args.dump_payload:
        print(json.dumps(payload, indent=2))
        return 0

    # 4. Optionally ask Claude for the narrative.
    narrative = None
    model_used = None
    if not args.no_ai:
        try:
            narrative = claude.generate_report(
                payload, api_key=args.api_key, model=args.model
            )
            model_used = args.model
        except claude.ClaudeError as exc:
            print(f"warning: AI narrative skipped: {exc}", file=sys.stderr)

    # 5. Render and emit the report.
    text = report.render(
        payload, narrative, repo_path=args.repo, model=model_used
    )
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        print(f"Report written to {args.out}", file=sys.stderr)
    else:
        print(text)
    return 0


def _resolve_subject(stats, args):
    if args.author:
        subject = gitstats.match_author(stats, args.author)
        if subject is None:
            print(
                f"error: no contributor matched '{args.author}'. "
                "Try --author with an email, or run with --dump-payload to "
                "inspect, or omit --author to use your git config.",
                file=sys.stderr,
            )
            return None
        return subject

    name, email = gitstats.detect_self(args.repo)
    for query in (email, name):
        if query:
            subject = gitstats.match_author(stats, query)
            if subject is not None:
                return subject

    print(
        "error: could not determine which contributor is 'you'. "
        "Pass --author <name-or-email> explicitly.",
        file=sys.stderr,
    )
    return None


if __name__ == "__main__":
    raise SystemExit(main())
