"""Disk cache for LLM responses, keyed on (model, messages, params). One JSON file per entry."""

import hashlib
import json
import os
import threading
import time
from pathlib import Path


class DiskCache:
    def __init__(self, root: Path):
        self.root = root

    @staticmethod
    def key(payload: dict) -> str:
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()

    def _path(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json"

    def get(self, key: str) -> dict | None:
        p = self._path(key)
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def set(self, key: str, value: dict) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(p, json.dumps(value, ensure_ascii=False))


def atomic_write(path: Path, text: str, tries: int = 20) -> None:
    """Write via a per-thread temp file + rename. Windows refuses a rename while another
    thread is replacing the same target, so retry briefly; same content either way."""
    tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    for i in range(tries):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.01 * (i + 1))
