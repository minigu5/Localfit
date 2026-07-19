"""Resolve a model name into a downloadable URL + filename.

Accepts three forms for `omm install <model_name>`:
  1. A curated short name (see CURATED_INDEX below), e.g. "tinyllama-1.1b-q4"
  2. An explicit HuggingFace ref "org/repo:filename.gguf"
  3. A direct https:// URL to a .gguf file
"""

from __future__ import annotations

from dataclasses import dataclass

import requests

from omm.featurize import parse_param_count_billions, parse_quant_bits

HF_API = "https://huggingface.co/api/models/{repo_id}"
HF_DOWNLOAD = "https://huggingface.co/{repo_id}/resolve/main/{filename}"

# Small curated index of popular GGUF models. Not exhaustive - `omm search`
# and `omm recommend` pull from a larger hosted candidate list instead.
CURATED_INDEX: dict[str, tuple[str, str]] = {
    "tinyllama-1.1b-q4": (
        "TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF",
        "tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf",
    ),
    "llama3.1-8b-instruct-q4": (
        "bartowski/Meta-Llama-3.1-8B-Instruct-GGUF",
        "Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf",
    ),
    "mistral-7b-instruct-q4": (
        "TheBloke/Mistral-7B-Instruct-v0.2-GGUF",
        "mistral-7b-instruct-v0.2.Q4_K_M.gguf",
    ),
}


class ModelResolutionError(Exception):
    pass


class AmbiguousModelError(ModelResolutionError):
    """Raised when a bare `org/repo` resolves to more than one .gguf file,
    so the caller can offer a quantization-level choice instead of just
    failing (see `rank_quant_variants`)."""

    def __init__(self, repo_id: str, candidates: list[str]):
        self.repo_id = repo_id
        self.candidates = candidates
        super().__init__(
            f"Repo '{repo_id}' has multiple .gguf files, specify one: "
            f"{repo_id}:<filename>\nOptions: {', '.join(candidates)}"
        )


@dataclass
class ResolvedModel:
    url: str
    filename: str
    repo_id: str | None  # None when installed from a direct URL (no HF repo)


@dataclass
class QuantVariant:
    filename: str
    quant_bits: float | None
    required_gb: float | None  # None when quant/param count couldn't be parsed
    fits: bool | None  # None when required_gb couldn't be estimated


_RAM_OVERHEAD_FACTOR = 1.2  # context/runtime slack on top of raw weight size


def rank_quant_variants(candidates: list[str], available_gb: float) -> list[QuantVariant]:
    """Rank a repo's .gguf files by hardware fit, best-fitting-and-highest-
    quality first, so the CLI can default the picker's cursor there."""
    variants = []
    for filename in candidates:
        quant_bits = parse_quant_bits(filename)
        param_b = parse_param_count_billions(filename)
        if quant_bits is not None and param_b is not None:
            required_gb = param_b * quant_bits / 8 * _RAM_OVERHEAD_FACTOR
            fits = required_gb <= available_gb
        else:
            required_gb = None
            fits = None
        variants.append(QuantVariant(filename, quant_bits, required_gb, fits))

    variants.sort(key=lambda v: (v.fits is not True, -(v.quant_bits or 0)))
    return variants


def _list_gguf_files(repo_id: str) -> list[str]:
    try:
        resp = requests.get(HF_API.format(repo_id=repo_id), timeout=15)
        resp.raise_for_status()
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        if status in (401, 403):
            raise ModelResolutionError(
                f"HF repo '{repo_id}' is private or gated - requires an access token."
            ) from e
        if status == 404:
            raise ModelResolutionError(f"HF repo '{repo_id}' not found.") from e
        raise ModelResolutionError(f"HF API request failed for '{repo_id}' ({status}).") from e
    except requests.RequestException as e:
        raise ModelResolutionError(f"Could not reach Hugging Face for '{repo_id}': {e}") from e

    siblings = resp.json().get("siblings", [])
    return [s["rfilename"] for s in siblings if s["rfilename"].endswith(".gguf")]


def resolve_model(model_name: str) -> ResolvedModel:
    if model_name in CURATED_INDEX:
        repo_id, filename = CURATED_INDEX[model_name]
        url = HF_DOWNLOAD.format(repo_id=repo_id, filename=filename)
        return ResolvedModel(url=url, filename=filename, repo_id=repo_id)

    if model_name.startswith("http://") or model_name.startswith("https://"):
        filename = model_name.rsplit("/", 1)[-1].split("?", 1)[0]
        return ResolvedModel(url=model_name, filename=filename, repo_id=None)

    if "/" in model_name:
        if ":" in model_name:
            repo_id, filename = model_name.split(":", 1)
            if not filename.lower().endswith(".gguf"):
                filename = f"{filename}.gguf"
        else:
            repo_id, filename = model_name, None
            candidates = _list_gguf_files(repo_id)
            if not candidates:
                raise ModelResolutionError(f"No .gguf files found in HF repo '{repo_id}'.")
            if len(candidates) > 1:
                raise AmbiguousModelError(repo_id, candidates)
            filename = candidates[0]
        url = HF_DOWNLOAD.format(repo_id=repo_id, filename=filename)
        return ResolvedModel(url=url, filename=filename, repo_id=repo_id)

    raise ModelResolutionError(
        f"Unknown model '{model_name}'. Use a curated name "
        f"({', '.join(CURATED_INDEX)}), an 'org/repo:file.gguf' ref, or a direct URL."
    )
