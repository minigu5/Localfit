# OMM CLI Convenience Update Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add brew-style `omm search`, install "did you mean" suggestions, `.gguf`-suffix-optional install/remove, Tab completion, `omm apply` (retroactive symlinking), `omm autoremove` (broken-symlink cleanup), and Escape-to-cancel on `omm recommend`.

**Architecture:** Two new pure-logic modules (`src/omm/search.py`, `src/omm/completion.py`) hold the reusable logic; `src/omm/linker.py` and `src/omm/hub.py` get small additions for their respective domains (broken-symlink cleanup, filename normalization); `src/omm/cli.py` stays the thin Typer orchestration layer and gains three new commands (`search`, `apply`, `autoremove`) plus small edits to `install`/`remove`/`recommend`.

**Tech Stack:** Python 3.10+, Typer 0.27 (vendors its own Click, supports legacy `autocompletion=` callback), questionary 2.1 (prompt_toolkit 3.0.52 under the hood), `requests`, stdlib `difflib`. Test stack: `pytest` (new dev-only dependency) + Typer's built-in `typer.testing.CliRunner`.

## Global Constraints

- No new **runtime** dependencies — `difflib` is stdlib, Click/Typer completion and `prompt_toolkit` key bindings are already transitively installed. Only `pytest` is added, as a dev-only extra.
- Python >=3.10 (unchanged, from `pyproject.toml`).
- Existing CLI output stays English/`rich`-markup styled (`console.print(f"[green]...[/green]")`); the one exception is the install "did you mean" header, which must be the exact Korean string `"이런 모델을 찾으셨나요?"` per the approved spec.
- Tab-completion callbacks must never make a network call (spec requirement: instant response) — they only read `hub.CURATED_INDEX` and the **already-cached** recommend-model artifact via `predictor.load_cached_model()`, never `predictor.load_model()` (which fetches live) or `search.local_candidate_pool()`.
- `omm search` and the install-failure suggestion path use `search.local_candidate_pool()` (curated + live-or-cached candidates) plus a short-timeout HuggingFace search; a HF network failure/timeout must degrade silently to local-only results, never crash the command.
- `omm autoremove` must never touch `~/.omm/models.json` (the omm registry) — it only deletes symlinks/manifests inside the LM Studio / Ollama data directories that no longer point at a real file.
- Spec doc: `docs/superpowers/specs/2026-07-19-cli-convenience-design.md` — consult it if a task description here seems ambiguous.

---

### Task 1: Test infrastructure bootstrap

**Files:**
- Modify: `pyproject.toml`
- Create: `tests/conftest.py`
- Create: `tests/test_smoke.py`

**Interfaces:**
- Produces: `isolated_omm_home` pytest fixture (in `tests/conftest.py`) — `(tmp_path, monkeypatch) -> Path`. Redirects `omm.config.OMM_HOME/MODELS_DIR/CONFIG_PATH/REGISTRY_PATH/RULES_PATH/RECOMMEND_MODEL_PATH`, `omm.registry.REGISTRY_PATH`, and `omm.cli.MODELS_DIR` into a throwaway `tmp_path`, then calls `config.ensure_omm_home()`. Used by Task 7's `apply` tests (any later task touching the registry/model files may reuse it).

- [ ] **Step 1: Add pytest as a dev dependency and configure test discovery**

Edit `pyproject.toml`, adding these two new top-level tables (after the existing `[project]` table's `dependencies` list, and after `[project.scripts]` respectively):

```toml
[project.optional-dependencies]
dev = ["pytest>=8"]
```

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
```

- [ ] **Step 2: Install the project with dev extras**

Run: `/Users/shinmingyu/Project/Localfit/.venv/bin/python3 -m pip install -e ".[dev]"`
Expected: pytest installs successfully (project is already an editable install per `omm-0.1.0.dist-info` in the venv, so this just adds pytest).

- [ ] **Step 3: Verify pytest runs (with zero tests, to confirm wiring)**

Run: `/Users/shinmingyu/Project/Localfit/.venv/bin/python3 -m pytest`
Expected: `collected 0 items` (no error) — confirms `testpaths = ["tests"]` resolved and pytest is importable.

- [ ] **Step 4: Create the shared fixture**

Create `tests/conftest.py`:

```python
"""Shared pytest fixtures for the omm test suite."""

from __future__ import annotations

import pytest

from omm import cli, config, registry


@pytest.fixture
def isolated_omm_home(tmp_path, monkeypatch):
    """Redirect all of omm's ~/.omm paths into a throwaway tmp_path so
    tests never touch (or depend on) the real user home directory."""
    home = tmp_path / ".omm"
    models_dir = home / "models"

    monkeypatch.setattr(config, "OMM_HOME", home)
    monkeypatch.setattr(config, "MODELS_DIR", models_dir)
    monkeypatch.setattr(config, "CONFIG_PATH", home / "config.json")
    monkeypatch.setattr(config, "REGISTRY_PATH", home / "models.json")
    monkeypatch.setattr(config, "RULES_PATH", home / "rules.json")
    monkeypatch.setattr(config, "RECOMMEND_MODEL_PATH", home / "recommend-model.json")

    monkeypatch.setattr(registry, "REGISTRY_PATH", config.REGISTRY_PATH)
    monkeypatch.setattr(cli, "MODELS_DIR", models_dir)

    config.ensure_omm_home()
    return home
```

- [ ] **Step 5: Write the smoke test**

Create `tests/test_smoke.py`:

```python
def test_pytest_runs() -> None:
    assert True
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `/Users/shinmingyu/Project/Localfit/.venv/bin/python3 -m pytest -v`
Expected: `tests/test_smoke.py::test_pytest_runs PASSED`, `1 passed`.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml tests/conftest.py tests/test_smoke.py
git commit -m "test: bootstrap pytest infrastructure"
```

---

### Task 2: `.gguf` suffix auto-normalization (install + remove)

**Files:**
- Modify: `src/omm/hub.py:64-79` (`resolve_model`'s `org/repo:filename` branch)
- Modify: `src/omm/cli.py:206-225` (`remove` command)
- Test: `tests/test_hub.py`
- Test: `tests/test_cli_remove.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: no new public functions — behavior change only. Later tasks don't depend on new names here.

- [ ] **Step 1: Write the failing test for `hub.resolve_model`**

Create `tests/test_hub.py`:

```python
from omm.hub import resolve_model


def test_resolve_model_appends_gguf_suffix_when_missing():
    resolved = resolve_model("bartowski/Meta-Llama-3.1-8B-Instruct-GGUF:Meta-Llama-3.1-8B-Instruct-Q4_K_M")

    assert resolved.filename == "Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf"
    assert resolved.url.endswith("Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf")


def test_resolve_model_leaves_existing_gguf_suffix_untouched():
    resolved = resolve_model("bartowski/Meta-Llama-3.1-8B-Instruct-GGUF:Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf")

    assert resolved.filename == "Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf"
```

- [ ] **Step 2: Run to verify it fails**

Run: `/Users/shinmingyu/Project/Localfit/.venv/bin/python3 -m pytest tests/test_hub.py -v`
Expected: `test_resolve_model_appends_gguf_suffix_when_missing` FAILS — `resolved.filename == "Meta-Llama-3.1-8B-Instruct-Q4_K_M"` (no `.gguf`).

- [ ] **Step 3: Implement the suffix normalization**

In `src/omm/hub.py`, inside `resolve_model`, change:

```python
    if "/" in model_name:
        if ":" in model_name:
            repo_id, filename = model_name.split(":", 1)
        else:
```

to:

```python
    if "/" in model_name:
        if ":" in model_name:
            repo_id, filename = model_name.split(":", 1)
            if not filename.lower().endswith(".gguf"):
                filename = f"{filename}.gguf"
        else:
```

- [ ] **Step 4: Run to verify it passes**

Run: `/Users/shinmingyu/Project/Localfit/.venv/bin/python3 -m pytest tests/test_hub.py -v`
Expected: both tests PASS.

- [ ] **Step 5: Write the failing test for `remove`**

Create `tests/test_cli_remove.py`:

```python
from typer.testing import CliRunner

from omm import cli, registry

runner = CliRunner()


def test_remove_accepts_filename_without_gguf_suffix(isolated_omm_home):
    filename = "tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf"
    dest = cli.MODELS_DIR / filename
    dest.write_bytes(b"fake-gguf")
    registry.save_registry({filename: {"linked": {"lmstudio": False, "ollama": False}}})

    result = runner.invoke(cli.app, ["remove", "tinyllama-1.1b-chat-v1.0.Q4_K_M"])

    assert result.exit_code == 0, result.stdout
    assert f"Removed {filename}" in result.stdout
    assert registry.load_registry() == {}
    assert not dest.exists()
```

- [ ] **Step 6: Run to verify it fails**

Run: `/Users/shinmingyu/Project/Localfit/.venv/bin/python3 -m pytest tests/test_cli_remove.py -v`
Expected: FAILS with `{filename} is not installed via omm` printed (exit_code 1), since `remove` currently does an exact-match lookup only.

- [ ] **Step 7: Implement the suffix fallback in `remove`**

In `src/omm/cli.py`, replace the start of the `remove` command:

```python
@app.command()
def remove(filename: str) -> None:
    """Remove a model and clean up all symlinks/manifests."""
    entry = registry.load_registry().get(filename)
    if entry is None:
        console.print(f"[red]{filename} is not installed via omm. See `omm list`.[/red]")
        raise typer.Exit(1)
```

with:

```python
@app.command()
def remove(filename: str) -> None:
    """Remove a model and clean up all symlinks/manifests."""
    reg = registry.load_registry()
    entry = reg.get(filename)
    if entry is None and not filename.lower().endswith(".gguf"):
        filename = f"{filename}.gguf"
        entry = reg.get(filename)
    if entry is None:
        console.print(f"[red]{filename} is not installed via omm. See `omm list`.[/red]")
        raise typer.Exit(1)
```

- [ ] **Step 8: Run to verify it passes**

Run: `/Users/shinmingyu/Project/Localfit/.venv/bin/python3 -m pytest tests/test_hub.py tests/test_cli_remove.py -v`
Expected: all 3 tests PASS.

- [ ] **Step 9: Commit**

```bash
git add src/omm/hub.py src/omm/cli.py tests/test_hub.py tests/test_cli_remove.py
git commit -m "feat: auto-append .gguf suffix in install/remove"
```

---

### Task 3: `src/omm/search.py` — candidate pool, family guessing, matching

**Files:**
- Create: `src/omm/search.py`
- Test: `tests/test_search.py`

**Interfaces:**
- Consumes: `omm.hub.CURATED_INDEX` (`dict[str, tuple[str, str]]`), `omm.predictor.load_model(url: str | None) -> dict | None`.
- Produces (used by Task 4, 5, 6):
  - `guess_family(text: str) -> str`
  - `local_candidate_pool(model_url: str | None) -> list[dict]` — each dict has at least `name`/`repo_id`/`filename`/`description` keys (some may be `None`/missing depending on source).
  - `search_huggingface(query: str, limit: int = 20, timeout: float = 3.0) -> list[dict]` — dicts shaped `{"name": repo_id, "repo_id": repo_id, "filename": None, "description": "HuggingFace"}`.
  - `match_candidates(pool: list[dict], query: str) -> list[dict]`
  - `suggest_similar(query: str, pool: list[dict], limit: int = 3) -> list[dict]`
  - `group_by_family(candidates: list[dict]) -> dict[str, list[dict]]`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_search.py`:

```python
from omm import search as search_mod


def test_guess_family_tinyllama():
    assert search_mod.guess_family("tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf") == "TinyLlama"


def test_guess_family_llama_not_confused_by_tinyllama_substring():
    assert search_mod.guess_family("Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf") == "Llama"


def test_guess_family_mistral():
    assert search_mod.guess_family("mistral-7b-instruct-v0.2.Q4_K_M.gguf") == "Mistral"


def test_guess_family_other_for_unknown_name():
    assert search_mod.guess_family("some-random-model-name") == "Other"


def test_local_candidate_pool_merges_curated_and_cached_and_dedupes(monkeypatch):
    monkeypatch.setattr(
        search_mod.predictor,
        "load_model",
        lambda url: {
            "candidates": [
                {
                    "name": "tinyllama-1.1b-q4",
                    "repo_id": "TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF",
                    "filename": "tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf",
                    "description": "Curated default",
                },
                {
                    "name": "qwen2.5-7b-instruct-q4",
                    "repo_id": "Qwen/Qwen2.5-7B-Instruct-GGUF",
                    "filename": "qwen2.5-7b-instruct-q4_k_m.gguf",
                    "description": "Solid 7B",
                },
            ]
        },
    )

    pool = search_mod.local_candidate_pool(None)

    repo_ids = [c["repo_id"] for c in pool]
    assert repo_ids.count("TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF") == 1
    assert "Qwen/Qwen2.5-7B-Instruct-GGUF" in repo_ids
    # 3 curated (tinyllama/llama3.1/mistral) + 1 new qwen from the cache = 4
    assert len(pool) == 4


def test_search_huggingface_returns_empty_list_on_request_error(monkeypatch):
    def _raise(*args, **kwargs):
        raise search_mod.requests.RequestException("boom")

    monkeypatch.setattr(search_mod.requests, "get", _raise)

    assert search_mod.search_huggingface("qwen") == []


def test_match_candidates_prefers_substring_match():
    pool = [
        {"name": "mistral-7b-instruct-q4", "repo_id": "TheBloke/Mistral-7B-Instruct-v0.2-GGUF"},
        {"name": "llama3.1-8b-instruct-q4", "repo_id": "bartowski/Meta-Llama-3.1-8B-Instruct-GGUF"},
    ]

    result = search_mod.match_candidates(pool, "mistral")

    assert [c["name"] for c in result] == ["mistral-7b-instruct-q4"]


def test_match_candidates_falls_back_to_fuzzy_match_on_typo():
    pool = [{"name": "mistral-7b-instruct-q4", "repo_id": "TheBloke/Mistral-7B-Instruct-v0.2-GGUF"}]

    result = search_mod.match_candidates(pool, "mistrall")

    assert result == pool


def test_suggest_similar_limits_and_orders_by_closeness():
    pool = [
        {"name": "tinyllama-1.1b-q4"},
        {"name": "llama3.1-8b-instruct-q4"},
        {"name": "mistral-7b-instruct-q4"},
    ]

    suggestions = search_mod.suggest_similar("tinylama-1.1b-q4", pool, limit=2)

    assert len(suggestions) <= 2
    assert suggestions[0]["name"] == "tinyllama-1.1b-q4"


def test_group_by_family_buckets_by_parsed_family():
    pool = [
        {"name": "tinyllama-1.1b-q4", "repo_id": "TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF"},
        {"name": "mistral-7b-instruct-q4", "repo_id": "TheBloke/Mistral-7B-Instruct-v0.2-GGUF"},
    ]

    groups = search_mod.group_by_family(pool)

    assert set(groups.keys()) == {"TinyLlama", "Mistral"}
    assert groups["TinyLlama"][0]["name"] == "tinyllama-1.1b-q4"
```

- [ ] **Step 2: Run to verify it fails**

Run: `/Users/shinmingyu/Project/Localfit/.venv/bin/python3 -m pytest tests/test_search.py -v`
Expected: FAILS with `ModuleNotFoundError: No module named 'omm.search'`.

- [ ] **Step 3: Implement `src/omm/search.py`**

Create `src/omm/search.py`:

```python
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
```

- [ ] **Step 4: Run to verify it passes**

Run: `/Users/shinmingyu/Project/Localfit/.venv/bin/python3 -m pytest tests/test_search.py -v`
Expected: all 9 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/omm/search.py tests/test_search.py
git commit -m "feat: add search module (candidate pool, family grouping, fuzzy match)"
```

---

### Task 4: Wire `omm search` command

**Files:**
- Modify: `src/omm/cli.py:11` (imports), append new `search` command near the end of the command list (after `list_models`, before `_report_telemetry`)
- Test: `tests/test_cli_search.py`

**Interfaces:**
- Consumes: `search.local_candidate_pool`, `search.match_candidates`, `search.search_huggingface`, `search.group_by_family` (Task 3).

- [ ] **Step 1: Write the failing test**

Create `tests/test_cli_search.py`:

```python
from typer.testing import CliRunner

from omm import cli, search as search_mod

runner = CliRunner()


def test_search_groups_results_by_family(monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda: {"model_url": None})
    monkeypatch.setattr(
        search_mod,
        "local_candidate_pool",
        lambda model_url: [
            {
                "name": "tinyllama-1.1b-q4",
                "repo_id": "TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF",
                "description": "Curated default",
            },
            {
                "name": "mistral-7b-instruct-q4",
                "repo_id": "TheBloke/Mistral-7B-Instruct-v0.2-GGUF",
                "description": "Curated default",
            },
        ],
    )
    monkeypatch.setattr(search_mod, "search_huggingface", lambda query, **kwargs: [])

    result = runner.invoke(cli.app, ["search", "q4"])

    assert result.exit_code == 0, result.stdout
    assert "==> TinyLlama" in result.stdout
    assert "==> Mistral" in result.stdout
    assert "tinyllama-1.1b-q4" in result.stdout
    assert "mistral-7b-instruct-q4" in result.stdout


def test_search_exits_nonzero_when_nothing_matches(monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda: {"model_url": None})
    monkeypatch.setattr(search_mod, "local_candidate_pool", lambda model_url: [])
    monkeypatch.setattr(search_mod, "search_huggingface", lambda query, **kwargs: [])

    result = runner.invoke(cli.app, ["search", "nonexistent-xyz"])

    assert result.exit_code == 1
    assert "No models found" in result.stdout
```

- [ ] **Step 2: Run to verify it fails**

Run: `/Users/shinmingyu/Project/Localfit/.venv/bin/python3 -m pytest tests/test_cli_search.py -v`
Expected: FAILS — `No such command 'search'`.

- [ ] **Step 3: Implement the command**

In `src/omm/cli.py`, change the import line:

```python
from omm import benchmark, linker, predictor, registry, rules as rules_mod, telemetry
```

to:

```python
from omm import benchmark, linker, predictor, registry, rules as rules_mod, search as search_mod, telemetry
```

Then add this command right after `list_models` (before the `_report_telemetry` helper):

```python
@app.command()
def search(query: str) -> None:
    """Search curated models, cached candidates, and HuggingFace by name."""
    config = load_config()
    pool = search_mod.local_candidate_pool(config.get("model_url"))
    local_matches = search_mod.match_candidates(pool, query)

    local_repo_ids = {c.get("repo_id") for c in local_matches if c.get("repo_id")}
    hf_matches = [
        c
        for c in search_mod.search_huggingface(query)
        if c.get("repo_id") not in local_repo_ids
    ]

    combined = local_matches + hf_matches
    if not combined:
        console.print(f"[yellow]No models found matching '{query}'.[/yellow]")
        raise typer.Exit(1)

    groups = search_mod.group_by_family(combined)
    for family in sorted(groups):
        console.print(f"[bold cyan]==> {family}[/bold cyan]")
        for c in groups[family]:
            label = c.get("name") or c.get("repo_id")
            desc = c.get("description") or ""
            console.print(f"  {label}  [dim]{desc}[/dim]")
        console.print()
```

- [ ] **Step 4: Run to verify it passes**

Run: `/Users/shinmingyu/Project/Localfit/.venv/bin/python3 -m pytest tests/test_cli_search.py -v`
Expected: both tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/omm/cli.py tests/test_cli_search.py
git commit -m "feat: add omm search command"
```

---

### Task 5: Install-failure "did you mean" suggestions

**Files:**
- Modify: `src/omm/cli.py:134-140` (`install` command's `except ModelResolutionError` branch), add a `_print_install_suggestions` helper near the bottom of the file (next to `_report_telemetry`)
- Test: `tests/test_cli_install_suggestions.py`

**Interfaces:**
- Consumes: `search_mod.local_candidate_pool`, `search_mod.suggest_similar`, `search_mod.search_huggingface` (Task 3).
- Produces: `_print_install_suggestions(query: str) -> None` (cli.py-private helper).

- [ ] **Step 1: Write the failing test**

Create `tests/test_cli_install_suggestions.py`:

```python
from typer.testing import CliRunner

from omm import cli, search as search_mod

runner = CliRunner()


def test_install_unknown_model_prints_did_you_mean_suggestions(monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda: {"model_url": None})
    monkeypatch.setattr(
        search_mod,
        "local_candidate_pool",
        lambda model_url: [
            {"name": "tinyllama-1.1b-q4", "repo_id": "TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF"},
        ],
    )
    monkeypatch.setattr(search_mod, "search_huggingface", lambda query, **kwargs: [])

    result = runner.invoke(cli.app, ["install", "tinylama-1.1b-q4"])

    assert result.exit_code == 1
    assert "이런 모델을 찾으셨나요?" in result.stdout
    assert "tinyllama-1.1b-q4" in result.stdout


def test_install_unknown_model_with_no_suggestions_still_exits_cleanly(monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda: {"model_url": None})
    monkeypatch.setattr(search_mod, "local_candidate_pool", lambda model_url: [])
    monkeypatch.setattr(search_mod, "search_huggingface", lambda query, **kwargs: [])

    result = runner.invoke(cli.app, ["install", "totally-unrelated-xyz"])

    assert result.exit_code == 1
    assert "이런 모델을 찾으셨나요?" not in result.stdout
```

- [ ] **Step 2: Run to verify it fails**

Run: `/Users/shinmingyu/Project/Localfit/.venv/bin/python3 -m pytest tests/test_cli_install_suggestions.py -v`
Expected: first test FAILS (no suggestion text printed); second test currently PASSES already (no suggestions ever printed) — that's fine, it'll still pass after the change.

- [ ] **Step 3: Implement the helper and wire it into `install`**

In `src/omm/cli.py`, change:

```python
    try:
        resolved = resolve_model(model_name)
    except ModelResolutionError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e
```

to:

```python
    try:
        resolved = resolve_model(model_name)
    except ModelResolutionError as e:
        console.print(f"[red]{e}[/red]")
        _print_install_suggestions(model_name)
        raise typer.Exit(1) from e
```

Then add this helper next to `_report_telemetry` (near the bottom of the file):

```python
def _print_install_suggestions(query: str) -> None:
    config = load_config()
    pool = search_mod.local_candidate_pool(config.get("model_url"))
    suggestions = search_mod.suggest_similar(query, pool, limit=3)

    existing_labels = {s.get("name") or s.get("repo_id") for s in suggestions}
    if len(suggestions) < 3:
        for hit in search_mod.search_huggingface(query, limit=5):
            if len(suggestions) >= 3:
                break
            label = hit.get("name") or hit.get("repo_id")
            if label in existing_labels:
                continue
            suggestions.append(hit)
            existing_labels.add(label)

    if not suggestions:
        return

    console.print("[yellow]이런 모델을 찾으셨나요?[/yellow]")
    for s in suggestions:
        console.print(f"  - {s.get('name') or s.get('repo_id')}")
```

- [ ] **Step 4: Run to verify it passes**

Run: `/Users/shinmingyu/Project/Localfit/.venv/bin/python3 -m pytest tests/test_cli_install_suggestions.py -v`
Expected: both tests PASS.

- [ ] **Step 5: Run the full suite so far**

Run: `/Users/shinmingyu/Project/Localfit/.venv/bin/python3 -m pytest -v`
Expected: all tests from Tasks 1-5 PASS.

- [ ] **Step 6: Commit**

```bash
git add src/omm/cli.py tests/test_cli_install_suggestions.py
git commit -m "feat: suggest similar models when omm install can't resolve a name"
```

---

### Task 6: Tab completion for `install`/`remove`

**Files:**
- Create: `src/omm/completion.py`
- Modify: `src/omm/cli.py:11` (imports), `install` and `remove` signatures
- Modify: `install.sh` (append a one-line hint)
- Test: `tests/test_completion.py`

**Interfaces:**
- Consumes: `hub.CURATED_INDEX`, `predictor.load_cached_model() -> dict | None`, `registry.load_registry() -> dict`.
- Produces: `complete_install_name(incomplete: str) -> list[str]`, `complete_remove_filename(incomplete: str) -> list[str]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_completion.py`:

```python
from omm import completion


def test_complete_install_name_includes_curated_names(monkeypatch):
    monkeypatch.setattr(completion.predictor, "load_cached_model", lambda: None)

    result = completion.complete_install_name("tiny")

    assert "tinyllama-1.1b-q4" in result


def test_complete_install_name_includes_cached_candidate_names(monkeypatch):
    monkeypatch.setattr(
        completion.predictor,
        "load_cached_model",
        lambda: {"candidates": [{"name": "qwen2.5-7b-instruct-q4"}]},
    )

    result = completion.complete_install_name("qwen")

    assert result == ["qwen2.5-7b-instruct-q4"]


def test_complete_install_name_filters_by_prefix(monkeypatch):
    monkeypatch.setattr(completion.predictor, "load_cached_model", lambda: None)

    result = completion.complete_install_name("mistral")

    assert result == ["mistral-7b-instruct-q4"]


def test_complete_remove_filename_reads_registry_and_filters(monkeypatch):
    monkeypatch.setattr(
        completion.registry,
        "load_registry",
        lambda: {
            "tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf": {},
            "mistral-7b-instruct-v0.2.Q4_K_M.gguf": {},
        },
    )

    result = completion.complete_remove_filename("tiny")

    assert result == ["tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `/Users/shinmingyu/Project/Localfit/.venv/bin/python3 -m pytest tests/test_completion.py -v`
Expected: FAILS with `ModuleNotFoundError: No module named 'omm.completion'`.

- [ ] **Step 3: Implement `src/omm/completion.py`**

Create `src/omm/completion.py`:

```python
"""Shell tab-completion callbacks for omm's Typer CLI. These must never
make a network call, so `install` completion reads only the already-cached
recommend-model artifact (never a live fetch)."""

from __future__ import annotations

from omm import hub, predictor, registry


def complete_install_name(incomplete: str) -> list[str]:
    names = set(hub.CURATED_INDEX.keys())

    artifact = predictor.load_cached_model()
    if artifact:
        names.update(c.get("name") for c in artifact.get("candidates", []) if c.get("name"))

    return sorted(n for n in names if n.startswith(incomplete))


def complete_remove_filename(incomplete: str) -> list[str]:
    filenames = registry.load_registry().keys()
    return sorted(f for f in filenames if f.startswith(incomplete))
```

- [ ] **Step 4: Run to verify it passes**

Run: `/Users/shinmingyu/Project/Localfit/.venv/bin/python3 -m pytest tests/test_completion.py -v`
Expected: all 4 tests PASS.

- [ ] **Step 5: Wire the callbacks into `install`/`remove`**

In `src/omm/cli.py`, change the import line:

```python
from omm import benchmark, linker, predictor, registry, rules as rules_mod, search as search_mod, telemetry
```

to:

```python
from omm import benchmark, linker, predictor, registry, rules as rules_mod, search as search_mod, telemetry
from omm.completion import complete_install_name, complete_remove_filename
```

Change the `install` signature:

```python
@app.command()
def install(model_name: str) -> None:
```

to:

```python
@app.command()
def install(
    model_name: str = typer.Argument(..., autocompletion=complete_install_name),
) -> None:
```

Change the `remove` signature:

```python
@app.command()
def remove(filename: str) -> None:
```

to:

```python
@app.command()
def remove(
    filename: str = typer.Argument(..., autocompletion=complete_remove_filename),
) -> None:
```

- [ ] **Step 6: Add the completion hint to `install.sh`**

In `install.sh`, after the existing final `echo "Try:  omm scan"` line, add:

```sh
echo "Tip: run 'omm --install-completion' once (then restart your shell) to enable Tab completion for install/remove."
```

- [ ] **Step 7: Run the full suite to confirm nothing broke**

Run: `/Users/shinmingyu/Project/Localfit/.venv/bin/python3 -m pytest -v`
Expected: all tests from Tasks 1-6 PASS (the `typer.Argument(...)` change doesn't alter positional CLI behavior, so Task 2's and Task 5's `install`/`remove` tests must still pass unchanged).

- [ ] **Step 8: Commit**

```bash
git add src/omm/completion.py src/omm/cli.py install.sh tests/test_completion.py
git commit -m "feat: add Tab completion for install/remove"
```

---

### Task 7: `omm apply` — retroactive relinking

**Files:**
- Modify: `src/omm/cli.py` — add `apply` command after `list_models`/`search` (anywhere before `_report_telemetry`)
- Test: `tests/test_cli_apply.py`

**Interfaces:**
- Consumes: `registry.load_registry()`, `registry.upsert_entry(filename, **fields)`, `linker.is_lmstudio_installed()`, `linker.is_ollama_installed()`, `linker.link_lmstudio(gguf_path, repo_id)`, `linker.link_ollama(gguf_path, model_name) -> bool`, `linker.sanitize_ollama_tag(filename)`, `linker.LinkError`. All already exist (see `src/omm/linker.py` and `src/omm/registry.py`).
- Uses fixture: `isolated_omm_home` (Task 1).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cli_apply.py`:

```python
from typer.testing import CliRunner

from omm import cli, linker, registry

runner = CliRunner()


def test_apply_relinks_entry_missing_lmstudio_link(isolated_omm_home, monkeypatch):
    filename = "tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf"
    dest = cli.MODELS_DIR / filename
    dest.write_bytes(b"fake-gguf")

    registry.save_registry(
        {
            filename: {
                "linked": {"lmstudio": False, "ollama": True},
                "repo_id": "TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF",
                "ollama_name": "tinyllama",
            }
        }
    )

    monkeypatch.setattr(linker, "is_lmstudio_installed", lambda: True)
    monkeypatch.setattr(linker, "is_ollama_installed", lambda: True)

    calls = []
    monkeypatch.setattr(
        linker,
        "link_lmstudio",
        lambda gguf_path, repo_id: calls.append((gguf_path, repo_id)),
    )

    result = runner.invoke(cli.app, ["apply"])

    assert result.exit_code == 0, result.stdout
    assert calls == [(dest, "TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF")]
    updated = registry.load_registry()[filename]
    assert updated["linked"]["lmstudio"] is True
    assert updated["linked"]["ollama"] is True  # untouched, stays True


def test_apply_skips_entry_whose_source_file_is_missing(isolated_omm_home, monkeypatch):
    registry.save_registry({"ghost.gguf": {"linked": {"lmstudio": False, "ollama": False}}})

    monkeypatch.setattr(linker, "is_lmstudio_installed", lambda: True)
    monkeypatch.setattr(linker, "is_ollama_installed", lambda: True)

    result = runner.invoke(cli.app, ["apply"])

    assert result.exit_code == 0, result.stdout
    assert "0 model(s) newly linked" in result.stdout
    assert "1 skipped" in result.stdout


def test_apply_with_empty_registry_reports_nothing_to_do(isolated_omm_home):
    result = runner.invoke(cli.app, ["apply"])

    assert result.exit_code == 0, result.stdout
    assert "No models installed" in result.stdout
```

- [ ] **Step 2: Run to verify it fails**

Run: `/Users/shinmingyu/Project/Localfit/.venv/bin/python3 -m pytest tests/test_cli_apply.py -v`
Expected: FAILS — `No such command 'apply'`.

- [ ] **Step 3: Implement the `apply` command**

In `src/omm/cli.py`, add this command after `search` (before `_report_telemetry`):

```python
@app.command()
def apply() -> None:
    """Retry linking any installed models that couldn't be linked before
    (e.g. LM Studio or Ollama was installed after `omm install` ran)."""
    reg = registry.load_registry()
    if not reg:
        console.print("No models installed via omm yet.")
        raise typer.Exit(0)

    linked_count = 0
    skipped_missing = 0
    already_ok = 0

    for filename, entry in reg.items():
        dest = MODELS_DIR / filename
        if not dest.exists():
            skipped_missing += 1
            continue

        linked = entry.get("linked", {})
        new_linked: dict[str, bool] = {}
        update_fields: dict[str, str] = {}
        changed = False

        if not linked.get("lmstudio") and linker.is_lmstudio_installed():
            try:
                linker.link_lmstudio(dest, entry.get("repo_id"))
                new_linked["lmstudio"] = True
                changed = True
            except linker.LinkError as e:
                console.print(f"[yellow]{filename}: LM Studio link skipped: {e}[/yellow]")

        if not linked.get("ollama") and linker.is_ollama_installed():
            ollama_tag = entry.get("ollama_name") or linker.sanitize_ollama_tag(filename)
            try:
                linker.link_ollama(dest, ollama_tag)
                new_linked["ollama"] = True
                update_fields["ollama_name"] = ollama_tag
                changed = True
            except linker.LinkError as e:
                console.print(f"[yellow]{filename}: Ollama link skipped: {e}[/yellow]")

        if changed:
            registry.upsert_entry(filename, linked=new_linked, **update_fields)
            linked_count += 1
        else:
            already_ok += 1

    console.print(
        f"[green]{linked_count} model(s) newly linked.[/green] "
        f"{already_ok} already up to date, {skipped_missing} skipped (file missing)."
    )
```

- [ ] **Step 4: Run to verify it passes**

Run: `/Users/shinmingyu/Project/Localfit/.venv/bin/python3 -m pytest tests/test_cli_apply.py -v`
Expected: all 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/omm/cli.py tests/test_cli_apply.py
git commit -m "feat: add omm apply for retroactive engine linking"
```

---

### Task 8: `linker.autoremove_lmstudio` / `linker.autoremove_ollama`

**Files:**
- Modify: `src/omm/linker.py` — add two new functions at the end of the file
- Test: `tests/test_linker_autoremove.py`

**Interfaces:**
- Consumes: existing `lmstudio_models_dir()`, `ollama_models_dir()` (already defined in `linker.py`).
- Produces (used by Task 9): `autoremove_lmstudio() -> int`, `autoremove_ollama() -> tuple[int, int]` (`(blobs_removed, manifests_removed)`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_linker_autoremove.py`:

```python
import json

from omm import linker


def test_autoremove_lmstudio_deletes_broken_symlink_and_prunes_empty_dirs(tmp_path, monkeypatch):
    models_dir = tmp_path / "lmstudio" / "models"
    broken_dir = models_dir / "TheBloke" / "TinyLlama-1.1B-Chat-v1.0-GGUF"
    broken_dir.mkdir(parents=True)
    broken_link = broken_dir / "tinyllama.gguf"
    broken_link.symlink_to(tmp_path / "does-not-exist.gguf")

    live_target = tmp_path / "real.gguf"
    live_target.write_bytes(b"data")
    live_dir = models_dir / "org" / "repo"
    live_dir.mkdir(parents=True)
    live_link = live_dir / "real.gguf"
    live_link.symlink_to(live_target)

    monkeypatch.setattr(linker, "lmstudio_models_dir", lambda: models_dir)

    removed = linker.autoremove_lmstudio()

    assert removed == 1
    assert not broken_link.is_symlink()
    assert not broken_dir.exists()  # emptied parent got pruned
    assert live_link.is_symlink()  # untouched


def test_autoremove_lmstudio_returns_zero_when_dir_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(linker, "lmstudio_models_dir", lambda: tmp_path / "missing")

    assert linker.autoremove_lmstudio() == 0


def test_autoremove_ollama_removes_broken_blob_and_its_manifest(tmp_path, monkeypatch):
    models_dir = tmp_path / "ollama"
    blobs_dir = models_dir / "blobs"
    blobs_dir.mkdir(parents=True)

    broken_digest_hex = "a" * 64
    broken_blob = blobs_dir / f"sha256-{broken_digest_hex}"
    broken_blob.symlink_to(tmp_path / "gone.gguf")

    live_digest_hex = "b" * 64
    live_blob = blobs_dir / f"sha256-{live_digest_hex}"
    live_target = tmp_path / "alive.gguf"
    live_target.write_bytes(b"data")
    live_blob.symlink_to(live_target)

    manifests_root = models_dir / "manifests" / "registry.ollama.ai" / "library"

    broken_manifest_dir = manifests_root / "broken-model"
    broken_manifest_dir.mkdir(parents=True)
    (broken_manifest_dir / "latest").write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "config": {"digest": f"sha256:{broken_digest_hex}", "size": 1},
                "layers": [{"digest": f"sha256:{broken_digest_hex}", "size": 1}],
            }
        )
    )

    live_manifest_dir = manifests_root / "alive-model"
    live_manifest_dir.mkdir(parents=True)
    (live_manifest_dir / "latest").write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "config": {"digest": f"sha256:{live_digest_hex}", "size": 1},
                "layers": [{"digest": f"sha256:{live_digest_hex}", "size": 1}],
            }
        )
    )

    monkeypatch.setattr(linker, "ollama_models_dir", lambda: models_dir)

    blobs_removed, manifests_removed = linker.autoremove_ollama()

    assert blobs_removed == 1
    assert manifests_removed == 1
    assert not broken_blob.exists()
    assert not (broken_manifest_dir / "latest").exists()
    assert live_blob.is_symlink()
    assert (live_manifest_dir / "latest").exists()


def test_autoremove_ollama_returns_zero_when_blobs_dir_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(linker, "ollama_models_dir", lambda: tmp_path / "missing")

    assert linker.autoremove_ollama() == (0, 0)
```

- [ ] **Step 2: Run to verify it fails**

Run: `/Users/shinmingyu/Project/Localfit/.venv/bin/python3 -m pytest tests/test_linker_autoremove.py -v`
Expected: FAILS — `AttributeError: module 'omm.linker' has no attribute 'autoremove_lmstudio'`.

- [ ] **Step 3: Implement the two functions**

Append to `src/omm/linker.py`:

```python
# --- Autoremove (broken symlink cleanup) ------------------------------


def autoremove_lmstudio() -> int:
    """Delete broken LM Studio symlinks (source .gguf no longer exists).
    Returns the number removed."""
    base = lmstudio_models_dir()
    if not base.exists():
        return 0

    removed = 0
    for path in list(base.rglob("*")):
        if path.is_symlink() and not path.exists():
            path.unlink()
            removed += 1
            for parent in (path.parent, path.parent.parent):
                try:
                    parent.rmdir()
                except OSError:
                    break
    return removed


def autoremove_ollama() -> tuple[int, int]:
    """Delete broken Ollama model-layer blob symlinks and any manifests
    that reference them. Returns (blobs_removed, manifests_removed)."""
    blobs_dir = ollama_models_dir() / "blobs"
    manifests_root = ollama_models_dir() / "manifests"
    if not blobs_dir.exists():
        return (0, 0)

    broken_digests = set()
    for blob in blobs_dir.iterdir():
        if blob.is_symlink() and not blob.exists():
            broken_digests.add(blob.name)
            blob.unlink()

    manifests_removed = 0
    if broken_digests and manifests_root.exists():
        for manifest_path in list(manifests_root.rglob("latest")):
            try:
                manifest = json.loads(manifest_path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            layer_digests = {
                layer["digest"].replace(":", "-") for layer in manifest.get("layers", [])
            }
            if layer_digests & broken_digests:
                manifest_path.unlink()
                manifests_removed += 1
                try:
                    manifest_path.parent.rmdir()
                except OSError:
                    pass

    return (len(broken_digests), manifests_removed)
```

- [ ] **Step 4: Run to verify it passes**

Run: `/Users/shinmingyu/Project/Localfit/.venv/bin/python3 -m pytest tests/test_linker_autoremove.py -v`
Expected: all 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/omm/linker.py tests/test_linker_autoremove.py
git commit -m "feat: add broken-symlink detection/cleanup to linker"
```

---

### Task 9: Wire `omm autoremove` command

**Files:**
- Modify: `src/omm/cli.py` — add `autoremove` command after `apply` (before `_report_telemetry`)
- Test: `tests/test_cli_autoremove.py`

**Interfaces:**
- Consumes: `linker.is_lmstudio_installed()`, `linker.is_ollama_installed()`, `linker.autoremove_lmstudio()`, `linker.autoremove_ollama()` (Task 8).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cli_autoremove.py`:

```python
from typer.testing import CliRunner

from omm import cli, linker

runner = CliRunner()


def test_autoremove_reports_zero_when_nothing_broken(monkeypatch):
    monkeypatch.setattr(linker, "is_lmstudio_installed", lambda: True)
    monkeypatch.setattr(linker, "is_ollama_installed", lambda: True)
    monkeypatch.setattr(linker, "autoremove_lmstudio", lambda: 0)
    monkeypatch.setattr(linker, "autoremove_ollama", lambda: (0, 0))

    result = runner.invoke(cli.app, ["autoremove"])

    assert result.exit_code == 0, result.stdout
    assert "No broken symlinks found" in result.stdout


def test_autoremove_reports_counts_from_both_engines(monkeypatch):
    monkeypatch.setattr(linker, "is_lmstudio_installed", lambda: True)
    monkeypatch.setattr(linker, "is_ollama_installed", lambda: True)
    monkeypatch.setattr(linker, "autoremove_lmstudio", lambda: 2)
    monkeypatch.setattr(linker, "autoremove_ollama", lambda: (1, 1))

    result = runner.invoke(cli.app, ["autoremove"])

    assert result.exit_code == 0, result.stdout
    assert "Removed 2 broken LM Studio symlink(s)" in result.stdout
    assert "1 broken Ollama blob(s)" in result.stdout


def test_autoremove_skips_uninstalled_engines(monkeypatch):
    monkeypatch.setattr(linker, "is_lmstudio_installed", lambda: False)
    monkeypatch.setattr(linker, "is_ollama_installed", lambda: False)
    lmstudio_calls = []
    ollama_calls = []
    monkeypatch.setattr(linker, "autoremove_lmstudio", lambda: lmstudio_calls.append(1) or 0)
    monkeypatch.setattr(linker, "autoremove_ollama", lambda: ollama_calls.append(1) or (0, 0))

    result = runner.invoke(cli.app, ["autoremove"])

    assert result.exit_code == 0, result.stdout
    assert lmstudio_calls == []
    assert ollama_calls == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `/Users/shinmingyu/Project/Localfit/.venv/bin/python3 -m pytest tests/test_cli_autoremove.py -v`
Expected: FAILS — `No such command 'autoremove'`.

- [ ] **Step 3: Implement the command**

In `src/omm/cli.py`, add this command after `apply` (before `_report_telemetry`):

```python
@app.command()
def autoremove() -> None:
    """Remove broken symlinks left behind when a model's source .gguf was
    deleted without going through `omm remove`."""
    lmstudio_removed = linker.autoremove_lmstudio() if linker.is_lmstudio_installed() else 0
    ollama_blobs_removed, ollama_manifests_removed = (
        linker.autoremove_ollama() if linker.is_ollama_installed() else (0, 0)
    )

    if lmstudio_removed == 0 and ollama_blobs_removed == 0:
        console.print("[green]No broken symlinks found.[/green]")
        return

    console.print(
        f"[green]Removed {lmstudio_removed} broken LM Studio symlink(s) and "
        f"{ollama_blobs_removed} broken Ollama blob(s) "
        f"({ollama_manifests_removed} manifest(s) cleaned up).[/green]"
    )
```

- [ ] **Step 4: Run to verify it passes**

Run: `/Users/shinmingyu/Project/Localfit/.venv/bin/python3 -m pytest tests/test_cli_autoremove.py -v`
Expected: all 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/omm/cli.py tests/test_cli_autoremove.py
git commit -m "feat: add omm autoremove command"
```

---

### Task 10: `omm recommend` — Escape cancels like Ctrl+C

**Files:**
- Modify: `src/omm/cli.py:1-16` (imports), `:103` and `:125` (the two `questionary.select(...).ask()` call sites inside `recommend`)
- Test: `tests/test_cli_recommend_escape.py`

**Interfaces:**
- Produces: `_add_escape_to_cancel(question: questionary.Question) -> questionary.Question`, `_ask_select(question: questionary.Question) -> str | None` (cli.py-private helpers).

- [ ] **Step 1: Write the failing test**

Create `tests/test_cli_recommend_escape.py`:

```python
from unittest.mock import MagicMock

import questionary
from prompt_toolkit.keys import Keys

from omm.cli import _add_escape_to_cancel


def test_escape_binding_triggers_keyboard_interrupt_style_exit():
    question = questionary.select(
        "Pick one:",
        choices=[questionary.Choice(title="a", value="a")],
    )

    _add_escape_to_cancel(question)

    escape_bindings = [
        b for b in question.application.key_bindings.bindings if b.keys == (Keys.Escape,)
    ]
    assert escape_bindings, "expected an Escape key binding to be registered"

    fake_event = MagicMock()
    escape_bindings[-1].handler(fake_event)

    fake_event.app.exit.assert_called_once_with(
        exception=KeyboardInterrupt, style="class:aborting"
    )
```

- [ ] **Step 2: Run to verify it fails**

Run: `/Users/shinmingyu/Project/Localfit/.venv/bin/python3 -m pytest tests/test_cli_recommend_escape.py -v`
Expected: FAILS — `ImportError: cannot import name '_add_escape_to_cancel' from 'omm.cli'`.

- [ ] **Step 3: Implement the helpers and wire them into `recommend`**

In `src/omm/cli.py`, add this import alongside the existing ones near the top of the file:

```python
from prompt_toolkit.keys import Keys
```

Add these two helpers directly above the `recommend` command:

```python
def _add_escape_to_cancel(question: questionary.Question) -> questionary.Question:
    """questionary only aborts on Ctrl+C/Ctrl+Q by default; make Escape do
    the same so `.ask()` returns None instead of requiring Ctrl+C."""

    def _abort(event) -> None:
        event.app.exit(exception=KeyboardInterrupt, style="class:aborting")

    question.application.key_bindings.add(Keys.Escape, eager=True)(_abort)
    return question


def _ask_select(question: questionary.Question):
    return _add_escape_to_cancel(question).ask()
```

Then, inside `recommend()`, replace both occurrences of:

```python
    selected = questionary.select("Pick a model to install:", choices=choices).ask()
```

with:

```python
    selected = _ask_select(questionary.select("Pick a model to install:", choices=choices))
```

(There are two occurrences — one in the trained-model branch, one in the static-rules fallback branch. Replace both; the surrounding `if selected is None: ... Cancelled ...` logic already handles the `None` result and needs no change.)

- [ ] **Step 4: Run to verify it passes**

Run: `/Users/shinmingyu/Project/Localfit/.venv/bin/python3 -m pytest tests/test_cli_recommend_escape.py -v`
Expected: PASSES.

- [ ] **Step 5: Run the full test suite**

Run: `/Users/shinmingyu/Project/Localfit/.venv/bin/python3 -m pytest -v`
Expected: every test from Tasks 1-10 PASSES.

- [ ] **Step 6: Manual smoke check of the CLI help output**

Run: `/Users/shinmingyu/Project/Localfit/.venv/bin/python3 -m omm.cli --help` won't work directly (no `__main__` guard for module execution) — instead run:
`cd /Users/shinmingyu/Project/Localfit && .venv/bin/python3 -c "from omm.cli import app; app(['--help'])"`
Expected: help text lists `apply`, `autoremove`, `search` alongside the original `scan`, `update`, `recommend`, `install`, `remove`, `list` commands, with no import errors.

- [ ] **Step 7: Commit**

```bash
git add src/omm/cli.py tests/test_cli_recommend_escape.py
git commit -m "feat: let Escape cancel the omm recommend model picker"
```

---

## Post-plan verification

After Task 10, run the full suite once more (`/Users/shinmingyu/Project/Localfit/.venv/bin/python3 -m pytest -v`) and confirm the command list via the Step 6 smoke check above — both should be clean before considering this plan done.
