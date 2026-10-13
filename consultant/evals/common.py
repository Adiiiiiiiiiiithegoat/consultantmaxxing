"""Shared eval helpers."""

import subprocess

from consultant.config import ROOT


def code_version() -> str:
    """Commit the results were produced with; '-dirty' if tracked files differ from it."""
    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                              check=False).stdout.strip()
    head = git("rev-parse", "HEAD") or "unknown"
    return head + ("-dirty" if git("status", "--porcelain", "--untracked-files=no") else "")
