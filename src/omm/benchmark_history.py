"""Cross-run dedup record of models already benchmarked on this machine
(`~/.omm/benchmark_history.json`), used by `omm contribute` so it never
re-benchmarks the same model twice. Global, not TTY-scoped (unlike
session_cache.py) - benchmarking is a real, expensive action that should
stay deduped across every terminal on this machine, not just one session.
Best-effort: never raises out of this module."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from omm import config
from omm.atomic import atomic_write_text, locked


def _path() -> Path:
    return config.OMM_HOME / "benchmark_history.json"


def _load() -> dict[str, Any]:
    path = _path()
    if not path.exists():
        return {"entries": {}}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {"entries": {}}
    return {"entries": dict(data.get("entries", {}))}


def _save(data: dict[str, Any]) -> None:
    try:
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps(data))
    except OSError:
        pass


def loaded_refs() -> set[str]:
    return set(_load()["entries"].keys())


def has_been_benchmarked(ref: str) -> bool:
    return ref in _load()["entries"]


def record_benchmarked(
    ref: str, *, repo_id: str | None, filename: str, sha256: str, tokens_per_sec: float
) -> None:
    path = _path()
    with locked(path):
        data = _load()
        data["entries"][ref] = {
            "repo_id": repo_id,
            "filename": filename,
            "sha256": sha256,
            "tokens_per_sec": tokens_per_sec,
            "benchmarked_at": datetime.now(timezone.utc).isoformat(),
        }
        _save(data)
