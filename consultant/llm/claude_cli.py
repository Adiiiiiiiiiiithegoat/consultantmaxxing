"""Claude through the Claude Code CLI on the user's subscription (no API key, no API credits).

Each call runs `claude -p` as a plain model call: the role's system prompt replaces Claude
Code's, every built-in tool is disabled (`--tools ""`), no MCP servers, no settings (so no
hooks or plugins from the user's setup), no skills, no saved session, an empty working
directory (no CLAUDE.md). The prompt goes in on stdin and the system prompt via a file, so
Windows' 32K command-line limit never bites. Output is `stream-json`: its final `result`
event is the same object `--output-format json` prints, and the stream also carries the
`rate_limit_event` with the subscription's 5-hour / 7-day window utilization.

Auth is OAuth (the logged-in subscription): ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN are
removed from the child environment, because either would make the CLI bill the API account.
`--bare` is not used for the same reason (it only accepts an API key).

Concurrency is 1 (a process-wide lock). When the usage limit is hit the call waits and
retries every `limit_wait` seconds (default 20 min) for up to `max_limit_wait`; work done so
far is already saved (workspace steps + response cache), so a killed run resumes cleanly.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

LOCK = threading.Lock()  # concurrency 1 across every thread in the process
_LIMIT = re.compile(r"usage limit|hit your limit|limit reached|out of (?:extra )?usage|"
                    r"rate[ _-]?limit|resets? (?:at )?\d", re.I)
STRIPPED_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


class UsageLimit(RuntimeError):
    """The subscription's usage window is exhausted."""


class CLIError(RuntimeError):
    """The CLI failed for another reason (crash, bad output, server error)."""


@dataclass
class CLIResult:
    content: str
    usage: dict             # prompt_tokens / completion_tokens / total_tokens (+ cache split)
    cost_usd: float         # what the call would cost at list price (not billed on a plan)
    finish_reason: str
    seconds: float
    windows: dict = field(default_factory=dict)  # {"five_hour": 0.7, "seven_day": 0.38}
    model: str = ""


def command(model: str, system_file: str, binary: str | None = None) -> list[str]:
    return [binary or shutil.which("claude") or "claude", "-p", "--output-format",
            "stream-json", "--verbose", "--model", model, "--system-prompt-file", system_file,
            "--tools", "", "--strict-mcp-config", "--setting-sources", "",
            "--disable-slash-commands", "--no-session-persistence"]


def render(messages: list[dict]) -> tuple[str, str]:
    """(system prompt, user prompt). A multi-turn exchange (e.g. a structured-output repair)
    is replayed as a transcript ending on the last user message."""
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    turns = [m for m in messages if m["role"] != "system"]
    if len(turns) == 1:
        return system, turns[0]["content"]
    convo = "\n\n".join(f"[{m['role'].upper()}]\n{m['content']}" for m in turns)
    return system, (f"The conversation so far:\n\n{convo}\n\nReply to the last USER message "
                    "only.")


def parse(stdout: str, stderr: str = "", returncode: int = 0) -> CLIResult:
    """Read the stream: the final result event plus the latest window utilization."""
    result, windows, rejected = None, {}, False
    for line in stdout.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") == "rate_limit_event":
            info = e.get("rate_limit_info") or {}
            rejected = rejected or info.get("status") == "rejected"
            windows = {k: v.get("utilization") for k, v in
                       (info.get("unifiedWindows") or {}).items()} or windows
        elif e.get("type") == "result":
            result = e
    text = (result or {}).get("result") or ""
    failed = result is None or bool(result.get("is_error")) or returncode != 0
    if failed and (rejected or _LIMIT.search(text) or _LIMIT.search(stderr)):
        raise UsageLimit(text or stderr[-300:] or "usage limit reached")
    if failed:
        raise CLIError((text or stderr[-500:] or f"exit {returncode}, no result").strip())
    u = result.get("usage") or {}
    cache_w, cache_r = u.get("cache_creation_input_tokens", 0), u.get("cache_read_input_tokens", 0)
    prompt = u.get("input_tokens", 0) + cache_w + cache_r
    out = u.get("output_tokens", 0)
    model = next(iter(result.get("modelUsage") or {}), "")
    return CLIResult(content=text, finish_reason="length" if result.get("stop_reason") ==
                     "max_tokens" else "stop", cost_usd=result.get("total_cost_usd") or 0.0,
                     seconds=(result.get("duration_ms") or 0) / 1000, windows=windows,
                     model=model, usage={"prompt_tokens": prompt, "completion_tokens": out,
                                         "total_tokens": prompt + out,
                                         "cache_write_tokens": cache_w,
                                         "cache_read_tokens": cache_r})


def default_runner(cmd: list[str], prompt: str, env: dict, cwd: str,
                   timeout: float = 1800) -> tuple[int, str, str]:
    p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=env, cwd=cwd, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def call(model: str, messages: list[dict], *, runner: Callable = default_runner,
         sleep: Callable = time.sleep, trace: Callable = lambda kind, **kw: None,
         limit_wait: float = 1200, max_limit_wait: float = 72 * 3600,
         max_retries: int = 3) -> CLIResult:
    """One plain model call. Waits out usage limits (every `limit_wait` s, up to
    `max_limit_wait`); retries other failures with backoff."""
    system, prompt = render(messages)
    env = {k: v for k, v in os.environ.items() if k not in STRIPPED_ENV}
    waited, failures = 0.0, 0
    with LOCK, tempfile.TemporaryDirectory(prefix="consult-claude-") as cwd:
        sys_file = Path(cwd) / "system.txt"
        sys_file.write_text(system or "You are a helpful assistant.", encoding="utf-8")
        cmd = command(model, str(sys_file))
        while True:
            try:
                code, out, err = runner(cmd, prompt, env, cwd)
                return parse(out, err, code)
            except UsageLimit as e:
                if waited >= max_limit_wait:
                    raise
                trace("usage_limit_wait", model=model, waited_s=waited, detail=str(e)[:200])
                print(f"[{time.strftime('%H:%M')}] Claude usage limit hit ({str(e)[:80]}); "
                      f"retrying in {limit_wait / 60:.0f} min (waited {waited / 3600:.1f} h)",
                      flush=True)
                sleep(limit_wait)
                waited += limit_wait
            except (CLIError, subprocess.TimeoutExpired, OSError) as e:
                failures += 1
                if failures > max_retries:
                    raise CLIError(f"{model}: {e}") from e
                trace("retry", model=model, attempt=failures, error=repr(e)[:300])
                sleep(min(60.0, 5.0 * 2 ** failures))
