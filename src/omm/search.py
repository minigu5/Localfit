"""Model search: local curated/cached candidates + live HuggingFace search,
merged, fuzzy-matched, and grouped by model family for `omm search` and for
"did you mean" suggestions on a failed `omm install`.
"""

from __future__ import annotations

import difflib
import re

import requests

from omm import hub, predictor

HF_SEARCH_API = "https://huggingface.co/api/models"

FAMILY_KEYWORDS: list[str] = [
    "TinyLlama",
    "Llama",
    "Mistral",
    "Mixtral",
    "Qwen",
    "Gemma",
    "Phi",
    "DeepSeek",
    "StableLM",
    "Falcon",
    "Yi",
]

# HF is full of spam repos claiming to be "distilled" or "fine-tuned" from
# closed, never-publicly-released weights (Claude/Opus, GPT-4/5, Gemini, ...).
# That's not a real technique - those weights were never downloadable, so the
# claim is fabricated. These repos exist to farm downloads/likes off famous
# names and ship broken or nonstandard GGUFs (fake architecture tags, garbage
# quantizations) that fail to load once a user actually installs them. Filter
# them out before they ever reach a suggestion.
FAKE_PROVENANCE_MARKERS: list[str] = [
    "claude",
    "anthropic",
    "opus",
    "sonnet-4",
    "sonnet4",
    "chatgpt",
    "gpt-4",
    "gpt4",
    "gpt-5",
    "gpt5",
    "gemini",
    "bard",
]


def _claims_fake_provenance(text: str) -> bool:
    lowered = text.lower()
    return any(
        re.search(rf"\b{re.escape(marker)}\b", lowered) for marker in FAKE_PROVENANCE_MARKERS
    )


def guess_family(text: str) -> str:
    for family in FAMILY_KEYWORDS:
        if re.search(rf"\b{re.escape(family)}\b", text, re.IGNORECASE):
            return family
    return "Other"


def _label(candidate: dict) -> str:
    return candidate.get("name") or candidate.get("repo_id") or ""


def _curated_as_candidates() -> list[dict]:
    return [
        {
            "name": name,
            "repo_id": repo_id,
            "filename": filename,
            "description": "Curated default",
        }
        for name, (repo_id, filename) in hub.CURATED_INDEX.items()
    ]


def local_candidate_pool(model_url: str | None) -> list[dict]:
    pool = _curated_as_candidates()
    artifact = predictor.load_model(model_url)
    if artifact:
        pool.extend(artifact.get("candidates", []))

    seen: set[tuple[str | None, str | None]] = set()
    deduped: list[dict] = []
    for candidate in pool:
        key = (candidate.get("repo_id"), candidate.get("filename"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return deduped


def search_huggingface(query: str, limit: int = 20, timeout: float = 3.0) -> list[dict]:
    try:
        resp = requests.get(
            HF_SEARCH_API,
            params={"search": query, "filter": "gguf", "limit": limit},
            timeout=timeout,
        )
        resp.raise_for_status()
        payload = resp.json()
    except (requests.RequestException, ValueError):
        return []

    results = []
    for item in payload:
        repo_id = item.get("id") or item.get("modelId")
        if not repo_id:
            continue
        if _claims_fake_provenance(repo_id):
            continue
        results.append(
            {
                "name": repo_id,
                "repo_id": repo_id,
                "filename": None,
                "description": "HuggingFace",
            }
        )
    return results


def match_candidates(pool: list[dict], query: str) -> list[dict]:
    q = query.lower()
    substring = [
        c
        for c in pool
        if q in _label(c).lower() or q in (c.get("repo_id") or "").lower()
    ]
    if substring:
        return substring

    labels = [_label(c) for c in pool]
    close = difflib.get_close_matches(query, labels, n=10, cutoff=0.4)
    return [c for c in pool if _label(c) in close]


def suggest_similar(query: str, pool: list[dict], limit: int = 3) -> list[dict]:
    labels = [_label(c) for c in pool]
    close = difflib.get_close_matches(query, labels, n=limit, cutoff=0.3)

    seen: set[str] = set()
    suggestions: list[dict] = []
    for c in pool:
        label = _label(c)
        if label in close and label not in seen:
            seen.add(label)
            suggestions.append(c)
        if len(suggestions) >= limit:
            break
    return suggestions


def group_by_family(candidates: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for c in candidates:
        text = f"{_label(c)} {c.get('repo_id') or ''}"
        family = guess_family(text)
        groups.setdefault(family, []).append(c)
    return groups
