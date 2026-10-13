"""Load YAML config with ${VAR:-default} env expansion. .env is loaded on import."""

import os
import re
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"

load_dotenv(ROOT / ".env")

_VAR = re.compile(r"\$\{(\w+)(?::-([^}]*))?\}")


def _expand(text: str) -> str:
    return _VAR.sub(lambda m: os.environ.get(m[1]) or (m[2] or ""), text)


def load_yaml(name: str) -> dict:
    return yaml.safe_load(_expand((CONFIG_DIR / name).read_text(encoding="utf-8")))


def models_config() -> dict:
    return load_yaml("models.yaml")


def profile(name: str = "dev") -> dict:
    return load_yaml("budgets.yaml")["profiles"][name]
