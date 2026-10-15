"""Originality guard for CLAUDE.md rule 1: a commit must not carry copyrighted source text.

Run by .githooks/pre-commit on the staged files (enable once per clone:
`git config core.hooksPath .githooks`). Blocks the commit when a staged text file shares a run
of N words with the source texts (the casebook and Bridgeway text extracted to data/derived,
and the extracted cases), or when a staged path is under data/ or is a PDF. N = 8: the
playbook test's 12-word runs missed an 11-word casebook quote that once reached PROGRESS.md.
Our own distilled notes (*_notes.md) are not sources. Without the local sources (a fresh
clone) only the path rules can run, and it says so.

Usage: python -m consultant.originality   (from the repo whose index is checked)
"""

import os
import re
import subprocess
import sys
from pathlib import Path

N = 8
BLOCKED_PATH = re.compile(r"(^|/)data/|\.pdf$", re.I)
DEFAULT_SOURCES = Path(__file__).resolve().parents[1] / "data" / "derived"


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def shingles(ws: list[str], n: int = N) -> set[tuple[str, ...]]:
    return {tuple(ws[i:i + n]) for i in range(len(ws) - n + 1)}


def source_shingles(root: Path, n: int = N) -> set[tuple[str, ...]]:
    files = list(root.glob("*.txt")) + list((root / "cases" / "all").glob("*.json"))
    return set().union(*(shingles(words(p.read_text(encoding="utf-8")), n) for p in files))


def overlaps(text: str, src: set, n: int = N) -> list[str]:
    """Shared runs of >= n words, merged and returned as text."""
    ws = words(text)
    idx = [i for i in range(len(ws) - n + 1) if tuple(ws[i:i + n]) in src]
    runs, start = [], None
    for k, i in enumerate(idx):
        start = i if start is None else start
        if k + 1 == len(idx) or idx[k + 1] > i + 1:
            runs.append(" ".join(ws[start:i + n]))
            start = None
    return runs


def check(files: list[tuple[str, bytes]], src: set) -> list[str]:
    problems = []
    for path, data in files:
        if BLOCKED_PATH.search(path):
            problems.append(f"{path}: source material / derived data path (data/, *.pdf)")
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for run in overlaps(text, src):
            problems.append(f"{path}: shares {len(run.split())} words with the sources: "
                            f"\"{run[:120]}\"")
    return problems


def staged(repo: Path) -> list[tuple[str, bytes]]:
    def git(*a) -> bytes:
        return subprocess.run(["git", *a], cwd=repo, capture_output=True, check=True).stdout
    names = git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z").split(b"\0")
    return [(n.decode(), git("show", f":{n.decode()}")) for n in names if n]


def main(argv=None) -> int:
    root = Path(os.environ.get("CONSULTANT_ORIGINALITY_SOURCES") or DEFAULT_SOURCES)
    if root.exists():
        src = source_shingles(root)
    else:
        print(f"originality: sources not found at {root}; only path rules checked",
              file=sys.stderr)
        src = set()
    problems = check(staged(Path.cwd()), src)
    for p in problems:
        print(f"originality: {p}", file=sys.stderr)
    if problems:
        print("originality: commit blocked (CLAUDE.md rule 1). Rewrite in your own words; "
              "never commit source text, case facts or data/ files.", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
