"""Load config/config.yaml and apply environment overrides from .env / os.environ."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Environment variables that may override config values (env wins over YAML).
ENV_OVERRIDES = {
    "DISPATCH_API_URL": ("dispatch_api", "url", str),
    "DISPATCH_MAX_RETRIES": ("dispatch_api", "max_retries", int),
    "START_MOCK_API": ("dispatch_api", "start_mock_api", lambda v: v.lower() in {"1", "true", "yes"}),
    "MAX_DATA_AGE_DAYS": ("run", "max_data_age_days", int),
    "WINDOW_DAYS": ("run", "window_days", int),
}


def _load_dotenv(path: Path) -> None:
    """Minimal .env reader so we don't need python-dotenv. Existing env vars are not overwritten."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


@dataclass(frozen=True)
class PipelineConfig:
    raw: Dict[str, Any]
    root: Path

    def section(self, name: str) -> Dict[str, Any]:
        return self.raw[name]

    def path(self, key: str) -> Path:
        return self.root / self.raw["paths"][key]

    @property
    def run(self):
        return self.raw["run"]

    @property
    def api(self):
        return self.raw["dispatch_api"]

    @property
    def validation(self):
        return self.raw["validation"]

    @property
    def late_definition(self):
        return self.raw["late_definition"]


def load_config(config_path: Path | None = None, root: Path = PROJECT_ROOT) -> PipelineConfig:
    config_path = config_path or root / "config" / "config.yaml"
    raw = yaml.safe_load(config_path.read_text())

    _load_dotenv(root / "config" / ".env")
    for env_key, (section, key, cast) in ENV_OVERRIDES.items():
        if env_key in os.environ:
            raw[section][key] = cast(os.environ[env_key])

    canonical = raw["late_definition"]["canonical"]
    if canonical not in raw["late_definition"]["definitions"]:
        raise ValueError(f"late_definition.canonical '{canonical}' is not a defined definition")

    return PipelineConfig(raw=raw, root=root)
