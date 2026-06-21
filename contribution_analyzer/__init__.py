"""Analyze a person's contributions to a git repository.

Gathers anonymized, non-confidential contribution *metadata* from a local git
repository and (optionally) asks the Claude API to write a strengths /
weaknesses / peer-comparison narrative — without ever exposing other
contributors' identities, commit message text, source code, or file paths.
"""

__version__ = "1.0.0"
