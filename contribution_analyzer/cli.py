"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__, anonymize, claude, gitstats, report


def build_parser():
    p = argparse.ArgumentParser(
        prog="contribution-analyzer",
        description="Analyze git contributions — per-person or whole-team. "
                    "No code, commit text, file paths, or peer identities are exposed.",
    )
    p.add_argument("--repo", default=".",
                   help="Local git repo (default: current dir).")
    p.add_argument("--author", default=None,
                   help="Name/email/fragment to analyze (default: your git config).")
    p.add_argument("--team", action="store_true",
                   help="Profile and rank every contributor across the codebase.")
    p.add_argument("--identify", action="store_true",
                   help="Use real names instead of 'Contributor #N' (opt-in).")
    p.add_argument("--top", type=int, default=None, metavar="N",
                   help="In --team mode, only the top N contributors.")
    p.add_argument("--min-commits", type=int, default=1, metavar="N",
                   help="In --team mode, skip authors with fewer than N commits.")
    p.add_argument("--branch", default=None,
                   help="Restrict to a branch/ref (default: HEAD history).")
    p.add_argument("--out", "-o", default=None,
                   help="Write Markdown here (default: stdout).")
    p.add_argument("--no-ai", action="store_true",
                   help="Skip the Claude narrative; metrics only.")
    p.add_argument("--model", default=claude.DEFAULT_MODEL,
                   help=f"Claude model id (default: {claude.DEFAULT_MODEL}).")
    p.add_argument("--api-key", default=None,
                   help="Anthropic API key (else ANTHROPIC_API_KEY).")
    p.add_argument("--dump-payload", action="store_true",
                   help="Print the JSON that would be sent to Claude, then exit.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)

    # The report uses a few non-ASCII chars; make sure stdout can handle them
    # on a legacy Windows code page.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass

    try:
        stats = gitstats.collect(args.repo, branch=args.branch)
    except gitstats.GitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not stats:
        print("error: no commits found in this repository.", file=sys.stderr)
        return 2

    if args.team:
        return run_team(stats, args)

    subject = resolve_subject(stats, args)
    if subject is None:
        return 2

    payload = anonymize.build_payload(stats, subject)
    if args.dump_payload:
        print(json.dumps(payload, indent=2))
        return 0

    narrative, model_used = maybe_narrative(
        lambda: claude.generate_report(payload, api_key=args.api_key, model=args.model),
        args,
    )
    return emit(report.render(payload, narrative, repo_path=args.repo, model=model_used), args)


def run_team(stats, args):
    payload = anonymize.build_team_payload(
        stats, identify=args.identify, top=args.top, min_commits=args.min_commits,
    )
    if not payload["contributors"]:
        print("error: no contributors met --min-commits; lower the threshold.",
              file=sys.stderr)
        return 2
    if args.dump_payload:
        print(json.dumps(payload, indent=2))
        return 0

    narrative, model_used = maybe_narrative(
        lambda: claude.generate_team_report(
            payload, identify=args.identify, api_key=args.api_key, model=args.model),
        args,
    )
    return emit(report.render_team(payload, narrative, repo_path=args.repo, model=model_used), args)


def maybe_narrative(call, args):
    """Run the Claude call unless --no-ai; degrade gracefully on error."""
    if args.no_ai:
        return None, None
    try:
        return call(), args.model
    except claude.ClaudeError as exc:
        print(f"warning: AI narrative skipped: {exc}", file=sys.stderr)
        return None, None


def emit(text, args):
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        print(f"Report written to {args.out}", file=sys.stderr)
    else:
        print(text)
    return 0


def resolve_subject(stats, args):
    if args.author:
        subject = gitstats.match_author(stats, args.author)
        if subject is None:
            print(f"error: no contributor matched '{args.author}'. Try an email, "
                  "or --dump-payload to inspect.", file=sys.stderr)
        return subject

    name, email = gitstats.detect_self(args.repo)
    for query in (email, name):
        if query:
            subject = gitstats.match_author(stats, query)
            if subject is not None:
                return subject

    print("error: couldn't tell which contributor is 'you'. Pass --author.",
          file=sys.stderr)
    return None


if __name__ == "__main__":
    raise SystemExit(main())
