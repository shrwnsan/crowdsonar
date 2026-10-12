"""Load and validate topic configs from YAML."""

import os
import re
from importlib import resources
from pathlib import Path

import yaml


def load_topic(name: str, configs_dir: str | None = None) -> dict:
    """Load a topic config by name (without .yaml extension)."""
    if configs_dir is None:
        ref = resources.files("crowdsonar") / "configs" / f"{name}.yaml"
        try:
            cfg = yaml.safe_load(ref.read_text(encoding="utf-8"))
        except FileNotFoundError as e:
            raise FileNotFoundError(f"Topic config not found: configs/{name}.yaml") from e
    else:
        path = Path(configs_dir) / f"{name}.yaml"
        if not path.exists():
            raise FileNotFoundError(f"Topic config not found: {path}")
        cfg = yaml.safe_load(path.read_text(encoding="utf-8"))

    _validate(cfg)
    return cfg


def list_topics(configs_dir: str | None = None) -> list[str]:
    """List available topic config names."""
    if configs_dir is None:
        configs = resources.files("crowdsonar") / "configs"
        return sorted(
            p.name[: -len(".yaml")]
            for p in configs.iterdir()
            if p.name.endswith(".yaml")
        )
    return sorted(p.stem for p in Path(configs_dir).glob("*.yaml"))


def _validate(cfg: dict) -> None:
    required = {"name", "sources", "signal_types", "synthesis"}
    missing = required - set(cfg.keys())
    if missing:
        raise ValueError(f"Topic config missing required keys: {missing}")

    # `name` keys every on-disk path (data/signals/<topic>_*.parquet,
    # data/raw/<topic>/) and lands inside DuckDB glob literals — restrict it
    # to a safe charset so a shared topic YAML can't traverse paths or break
    # out of a string literal.
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", str(cfg["name"])):
        raise ValueError(
            "topic name must match [A-Za-z0-9][A-Za-z0-9._-]* "
            f"(got: {cfg['name']!r})"
        )

    reddit = cfg["sources"].get("reddit")
    if reddit:
        if not reddit.get("subreddits"):
            raise ValueError("reddit.subreddits must be a non-empty list")
        fh = reddit.get("freshness_hours")
        if fh is not None and (isinstance(fh, bool) or not isinstance(fh, (int, float)) or fh <= 0):
            raise ValueError(
                "reddit.freshness_hours must be a positive number of hours "
                f"(got: {fh!r})"
            )
