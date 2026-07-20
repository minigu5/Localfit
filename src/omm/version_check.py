"""Best-effort, TTL-cached remote-HEAD lookup backing the background
"update available" notice (see cli.py's `_maybe_start_update_check`).
Never raises; a cache miss/failure just means the next `omm` invocation
tries the network fetch again once the TTL expires."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable

from omm import config

_TTL_SECONDS = 30 * 60


def _cache_path() -> Path:
    return config.OMM_HOME / "update_check.json"


def _load() -> dict:
    path = _cache_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    try:
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
    except OSError:
        pass


def cached_remote_head(
    fetch: Callable[[str], str | None],
    ref: str = "main",
    ttl_seconds: int = _TTL_SECONDS,
) -> str | None:
    """`fetch` is injected (cli._remote_head_commit) so the actual
    `git ls-remote` call stays single-sourced in cli.py. A `None` result
    (offline/unreachable) is cached too, same TTL, so an offline run
    doesn't retry the network call on every command."""
    cache = _load()
    checked_at = cache.get("checked_at")
    if isinstance(checked_at, (int, float)) and time.time() - checked_at < ttl_seconds:
        return cache.get("remote_head")
    latest = fetch(ref)
    _save({"checked_at": time.time(), "remote_head": latest})
    return latest
