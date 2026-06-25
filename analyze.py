#!/usr/bin/env python3
"""Thin entry point so the tool runs without installation:

    python analyze.py --repo /path/to/repo
"""

from contribution_analyzer.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
