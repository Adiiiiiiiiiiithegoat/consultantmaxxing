"""Originality pre-commit hook (CLAUDE.md rule 1). Sources here are synthetic stand-ins."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from consultant.originality import check, overlaps, shingles, words

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ("The harbour board raised berth rents twice in one season while the dredging contract "
          "lapsed, so the larger vessels quietly moved their calls to the rival terminal down the "
          "coast.")
COPIED = "Our note: the dredging contract lapsed, so the larger vessels quietly moved away."
CLEAN = "Rents went up and the channel silted, so big ships left; we checked the berth logs."


def test_check_blocks_shared_runs_and_source_paths():
    src = shingles(words(SOURCE))
    assert overlaps(COPIED, src) == ["the dredging contract lapsed so the larger vessels quietly "
                                     "moved"]
    assert overlaps("the dredging contract lapsed so the larger vessels", src)   # 8 words
    assert not overlaps("the dredging contract lapsed so the larger", src)       # 7 words
    assert check([("notes.md", COPIED.encode())], src)
    assert check([("notes.md", CLEAN.encode())], src) == []
    assert check([("data/derived/cases/x.json", b"{}"), ("a/guide.PDF", b"%PDF")], set())
    assert check([("img.png", b"\x89PNG\xff\xfe")], src) == []                   # binary: skip


def test_hook_is_versioned_and_runs_the_check():
    hook = (ROOT / ".githooks" / "pre-commit").read_text(encoding="utf-8")
    assert hook.startswith("#!/bin/sh") and "python -m consultant.originality" in hook


@pytest.mark.skipif(not (shutil.which("git") and shutil.which("uv")), reason="needs git and uv")
def test_hook_blocks_a_real_commit(tmp_path):
    src_dir = tmp_path / "sources"
    src_dir.mkdir()
    (src_dir / "casebook.txt").write_text(SOURCE, encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {**os.environ, "CONSULTANT_ORIGINALITY_SOURCES": str(src_dir),
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}

    def git(*a):
        return subprocess.run(["git", *a], cwd=repo, env=env, capture_output=True, text=True)
    git("init", "-q")
    git("config", "core.hooksPath", (ROOT / ".githooks").as_posix())
    (repo / "notes.md").write_text(COPIED, encoding="utf-8")
    git("add", "notes.md")
    blocked = git("commit", "-q", "-m", "copied")
    assert blocked.returncode != 0 and "commit blocked" in blocked.stderr
    (repo / "notes.md").write_text(CLEAN, encoding="utf-8")
    git("add", "notes.md")
    ok = git("commit", "-q", "-m", "own words")
    assert ok.returncode == 0, ok.stderr
