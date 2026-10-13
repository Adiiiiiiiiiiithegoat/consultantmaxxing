"""Run analysis code in a subprocess: timeout, memory cap, no network, own working dir.
Every run's code, stdout/stderr and RESULT are saved, so every number is reproducible.

Contract for analysis code: assign a JSON-serialisable dict to `RESULT`. The analysis
templates (`consultant.analysis.templates`) are importable inside the sandbox.

ponytail: the network block is a socket monkeypatch and the memory cap a psutil watchdog —
enough for LLM-written analysis code, not a security boundary. Use a container/VM if the
code could be adversarial.
"""

import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import psutil

BOOTSTRAP = r'''
import json, socket, sys
def _blocked(*a, **k):
    raise PermissionError("network access is disabled in the analysis sandbox")
socket.socket.connect = _blocked
socket.socket.connect_ex = _blocked
socket.create_connection = _blocked
socket.getaddrinfo = _blocked
code = open("code.py", encoding="utf-8").read()
ns = {"__name__": "__main__"}
exec(compile(code, "code.py", "exec"), ns)
res = ns.get("RESULT")
if res is not None:
    with open("result.json", "w", encoding="utf-8") as f:
        json.dump(res, f, indent=1, default=float)
'''


@dataclass
class RunResult:
    ok: bool
    result: dict | None
    stdout: str
    stderr: str
    seconds: float
    workdir: Path
    error: str | None = None


def _tree(ps: psutil.Process) -> list[psutil.Process]:
    """The process plus descendants: on Windows a venv python.exe is a launcher that runs the
    real interpreter as a child, so the child is what uses memory and must be killed."""
    try:
        return [ps, *ps.children(recursive=True)]
    except psutil.NoSuchProcess:
        return []


def _tree_memory(ps: psutil.Process) -> int:
    total = 0
    for p in _tree(ps):
        try:
            mi = p.memory_info()
            # Windows: committed private bytes (untouched zero pages aren't in RSS)
            total += max(mi.rss, getattr(mi, "private", 0))
        except psutil.NoSuchProcess:
            pass
    return total


def _kill_tree(ps: psutil.Process) -> None:
    for p in reversed(_tree(ps)):  # children first
        try:
            p.kill()
        except psutil.NoSuchProcess:
            pass


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:40] or "analysis"


def run(code: str, workdir: Path, name: str = "analysis", timeout: float = 30,
        mem_mb: int = 512) -> RunResult:
    """Execute `code` in `workdir/<nnn>_<name>/`; returns RESULT and captured output."""
    workdir.mkdir(parents=True, exist_ok=True)
    n = len([p for p in workdir.iterdir() if p.is_dir()]) + 1
    d = workdir / f"{n:03d}_{_slug(name)}"
    d.mkdir()
    (d / "code.py").write_text(code, encoding="utf-8")
    t0 = time.time()
    proc = subprocess.Popen([sys.executable, "-I", "-c", BOOTSTRAP], cwd=d,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace")
    error = None
    ps = psutil.Process(proc.pid)
    try:
        while proc.poll() is None:
            if time.time() - t0 > timeout:
                error = f"timed out after {timeout}s"
                break
            if _tree_memory(ps) > mem_mb * 1024 * 1024:
                error = f"exceeded memory cap of {mem_mb} MB"
                break
            time.sleep(0.05)
    finally:
        if proc.poll() is None:
            _kill_tree(ps)
        stdout, stderr = proc.communicate()
    secs = time.time() - t0
    if error is None and proc.returncode != 0:
        error = (stderr.strip().splitlines() or [f"exit code {proc.returncode}"])[-1]
    result = None
    if error is None and (d / "result.json").exists():
        result = json.loads((d / "result.json").read_text(encoding="utf-8"))
    (d / "stdout.txt").write_text(stdout, encoding="utf-8")
    (d / "stderr.txt").write_text(stderr, encoding="utf-8")
    (d / "run.json").write_text(json.dumps({"name": name, "ok": error is None, "error": error,
                                            "seconds": round(secs, 3)}), encoding="utf-8")
    return RunResult(error is None, result, stdout, stderr, secs, d, error)
