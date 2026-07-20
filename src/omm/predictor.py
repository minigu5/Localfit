"""Fetches the CI-trained recommendation model and ranks candidate GGUF
models against local hardware. Falls back through: live fetch -> last
cached copy -> the legacy heuristic rules (see rules.py) if neither is
available, so `omm recommend` always has something to show.
"""

from __future__ import annotations

import json

import requests

from omm.config import RECOMMEND_MODEL_PATH
from omm.featurize import build_features, parse_chip_score, parse_param_count_billions, parse_quant_bits
from omm.hardware import HardwareInfo, calculate_memory_budget
from omm.mltree import predict_ensemble


class ModelFetchError(Exception):
    pass


MODEL_MEMORY_OVERHEAD = 1.2


def available_model_memory_gb(hw: HardwareInfo) -> float:
    """Live model budget after current application use and safety reserves."""
    return calculate_memory_budget(hw).model_budget_gb


def estimate_required_memory_gb(candidate: dict) -> float | None:
    """Estimate model weights plus runtime overhead from verified metadata."""
    size_bytes = candidate.get("size_bytes")
    if isinstance(size_bytes, (int, float)) and size_bytes > 0:
        return float(size_bytes) / (1024**3) * MODEL_MEMORY_OVERHEAD

    text = (
        f"{candidate.get('name', '')} "
        f"{candidate.get('filename', '')} "
        f"{candidate.get('repo_id', '')}"
    )
    parameters = parse_param_count_billions(text)
    quant_bits = parse_quant_bits(text)
    if parameters is None or quant_bits is None:
        return None
    return parameters * quant_bits / 8.0 * 1.1 * MODEL_MEMORY_OVERHEAD


def candidate_fits_memory(hw: HardwareInfo, candidate: dict) -> bool | None:
    required = estimate_required_memory_gb(candidate)
    if required is None:
        return None
    return required <= available_model_memory_gb(hw)


def fetch_and_cache_model(url: str) -> dict:
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    artifact = resp.json()
    RECOMMEND_MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    RECOMMEND_MODEL_PATH.write_text(json.dumps(artifact))
    return artifact


def load_cached_model() -> dict | None:
    if not RECOMMEND_MODEL_PATH.exists():
        return None
    return json.loads(RECOMMEND_MODEL_PATH.read_text())


def load_model(url: str | None) -> dict | None:
    """Live fetch first, falling back to the last cached copy."""
    if url:
        try:
            return fetch_and_cache_model(url)
        except (requests.RequestException, ValueError):
            pass
    return load_cached_model()


def load_model_with_change_note(url: str | None) -> tuple[dict | None, bool]:
    """Like load_model, but also reports whether the result differs from
    what was already cached, so callers can tell the user when fresh data
    was actually pulled from the network (vs. an unchanged or failed fetch)."""
    previous = load_cached_model()
    artifact = load_model(url)
    return artifact, artifact != previous


def predict_speed(trees: list[dict], hw: HardwareInfo, candidate: dict) -> float:
    """Predicted tokens/sec for one candidate on this hardware. <= 0 means
    the trained model expects it not to run (OOM or similarly unviable)."""
    if candidate_fits_memory(hw, candidate) is False:
        return 0.0
    has_gpu = hw.vram_total_gb is not None
    text = f"{candidate.get('name', '')} {candidate.get('filename', '')} {candidate.get('repo_id', '')}"
    cpu_score, cpu_tier = parse_chip_score(hw.cpu or "")
    gpu_score, gpu_tier = parse_chip_score(hw.gpu_name or "")
    features = build_features(
        ram_gb=hw.ram_total_gb,
        vram_gb=hw.vram_total_gb if has_gpu else 0.0,
        unified_memory=hw.unified_memory,
        param_count_b=parse_param_count_billions(text),
        quant_bits=parse_quant_bits(text),
        cpu_score=cpu_score,
        cpu_tier=cpu_tier,
        gpu_score=gpu_score,
        gpu_tier=gpu_tier,
    )
    return predict_ensemble(trees, features)


def rank_candidates(artifact: dict, hw: HardwareInfo) -> list[tuple[dict, float]]:
    trees = artifact["trees"]
    ranked = [
        (candidate, predict_speed(trees, hw, candidate))
        for candidate in artifact["candidates"]
    ]
    ranked.sort(key=lambda pair: pair[1], reverse=True)
    return ranked
