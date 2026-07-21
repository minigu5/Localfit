# Quant Picker Predicted-Best Highlight Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** In the ambiguous-repo quant picker (`omm install <repo>` with multiple `.gguf` files), highlight the predicted-fastest variant within each quant-bits tier in green, using the existing ML speed predictor.

**Architecture:** A new pure function in `hub.py` picks the fastest filename per quant-bits tier given a `{filename: predicted_speed}` map (no dependency on `predictor`/`hardware`, easy to unit test). A new helper in `cli.py` builds that map by calling `predictor.load_cached_model()` + `predictor.predict_speed()` for each hardware-fitting variant, returning an empty set if no model is cached. `_pick_quant_variant` renders tier-best filenames as green `questionary.Choice` titles.

**Tech Stack:** Python, questionary (prompt_toolkit formatted-text tuples for color), pytest.

## Global Constraints

- No cached predictor model → skip all green marking, render exactly as today (spec: docs/superpowers/specs/2026-07-21-quant-picker-predicted-best-design.md).
- Variant excluded from tier-best consideration if `fits is not True` or predicted speed is `<= 0` (unparseable/unviable) — never force a green guess.
- Tie-break: first variant in the existing sort order (fits-desc, quant_bits-desc) wins ties — achieved by strict `>` comparison during iteration, no extra tie-break code.
- Sort order of choices is unchanged; green is a color overlay only.
- Green rendering: `questionary.Choice(title=[("fg:green bold", f"{filename}  ({note}, predicted fastest)")], value=filename)`.

---

### Task 1: `best_filenames_by_tier` in `hub.py`

**Files:**
- Modify: `src/omm/hub.py` (add function near `rank_quant_variants`, after the `QuantVariant` dataclass block ending at line 102)
- Test: `tests/test_hub_quant_variants.py` (append)

**Interfaces:**
- Produces: `hub.best_filenames_by_tier(variants: list[QuantVariant], predicted_speed: dict[str, float]) -> set[str]`
  - `predicted_speed` maps filename -> predicted speed (only for variants that were fit-and-resolvable; callers omit everything else).
  - Returns the set of filenames that are the fastest in their `quant_bits` tier among filenames present in `predicted_speed`.
  - A variant whose `filename` is absent from `predicted_speed`, or whose `quant_bits is None`, is never returned.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_hub_quant_variants.py`:

```python
def test_best_filenames_by_tier_picks_fastest_per_quant_tier():
    variants = [
        hub.QuantVariant("q4-a.gguf", quant_bits=4.0, required_gb=4.0, fits=True),
        hub.QuantVariant("q4-b.gguf", quant_bits=4.0, required_gb=4.0, fits=True),
        hub.QuantVariant("q5-a.gguf", quant_bits=5.0, required_gb=5.0, fits=True),
        hub.QuantVariant("q5-b.gguf", quant_bits=5.0, required_gb=5.0, fits=True),
    ]
    predicted_speed = {
        "q4-a.gguf": 10.0,
        "q4-b.gguf": 12.0,
        "q5-a.gguf": 8.0,
        "q5-b.gguf": 6.0,
    }

    best = hub.best_filenames_by_tier(variants, predicted_speed)

    assert best == {"q4-b.gguf", "q5-a.gguf"}


def test_best_filenames_by_tier_ties_resolve_to_first_in_list_order():
    variants = [
        hub.QuantVariant("first.gguf", quant_bits=4.0, required_gb=4.0, fits=True),
        hub.QuantVariant("second.gguf", quant_bits=4.0, required_gb=4.0, fits=True),
    ]
    predicted_speed = {"first.gguf": 10.0, "second.gguf": 10.0}

    best = hub.best_filenames_by_tier(variants, predicted_speed)

    assert best == {"first.gguf"}


def test_best_filenames_by_tier_ignores_variants_missing_from_speed_map():
    variants = [
        hub.QuantVariant("known.gguf", quant_bits=4.0, required_gb=4.0, fits=True),
        hub.QuantVariant("unknown.gguf", quant_bits=4.0, required_gb=4.0, fits=None),
    ]
    predicted_speed = {"known.gguf": 10.0}

    best = hub.best_filenames_by_tier(variants, predicted_speed)

    assert best == {"known.gguf"}


def test_best_filenames_by_tier_empty_speed_map_returns_empty_set():
    variants = [hub.QuantVariant("a.gguf", quant_bits=4.0, required_gb=4.0, fits=True)]

    assert hub.best_filenames_by_tier(variants, {}) == set()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_hub_quant_variants.py -v -k best_filenames_by_tier`
Expected: FAIL with `AttributeError: module 'omm.hub' has no attribute 'best_filenames_by_tier'`

- [ ] **Step 3: Implement `best_filenames_by_tier`**

In `src/omm/hub.py`, immediately after the `rank_quant_variants` function (after line 102, before `_fetch_repo_gguf_info`):

```python
def best_filenames_by_tier(
    variants: list[QuantVariant], predicted_speed: dict[str, float]
) -> set[str]:
    """Fastest filename per quant_bits tier, using only the (repo_id,
    filename) pairs the caller already resolved a predicted speed for -
    every other variant (didn't fit, speed unresolvable) is left out of
    consideration entirely rather than guessed at.

    Ties keep whichever filename appears first in `variants` (already
    sorted fits-desc/quant_bits-desc by `rank_quant_variants`), via the
    strict `>` below.
    """
    best_for_tier: dict[float, tuple[str, float]] = {}
    for variant in variants:
        if variant.quant_bits is None:
            continue
        speed = predicted_speed.get(variant.filename)
        if speed is None:
            continue
        current = best_for_tier.get(variant.quant_bits)
        if current is None or speed > current[1]:
            best_for_tier[variant.quant_bits] = (variant.filename, speed)
    return {filename for filename, _ in best_for_tier.values()}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_hub_quant_variants.py -v -k best_filenames_by_tier`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add src/omm/hub.py tests/test_hub_quant_variants.py
git commit -m "feat: add best_filenames_by_tier for quant picker highlighting"
```

---

### Task 2: Wire predictor into `_pick_quant_variant` + green rendering

**Files:**
- Modify: `src/omm/cli.py:718-760` (`_pick_quant_variant`), imports around line 52
- Test: `tests/test_cli_install_quant_picker.py` (append)

**Interfaces:**
- Consumes: `hub.best_filenames_by_tier(variants, predicted_speed)` from Task 1; `predictor.load_cached_model() -> dict | None`; `predictor.predict_speed(trees, hw, candidate: dict) -> float` (existing, `src/omm/predictor.py:171-175`).
- Produces: `cli._predicted_fastest_filenames(variants: list[QuantVariant], repo_id: str, hw: HardwareInfo) -> set[str]` (new helper, used only by `_pick_quant_variant`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli_install_quant_picker.py`:

```python
def test_quant_picker_marks_predicted_fastest_variant_green(isolated_omm_home, monkeypatch):
    # _HARDWARE has ram_total_gb=6.0 -> model budget is min(6.0*0.8, 6.0-2.0) = 4.0GB
    # (see hardware.calculate_memory_budget). A 3B model at Q4 needs
    # 3 * 4 / 8 * 1.2 = 1.8GB, comfortably under that budget so both variants
    # land in the same "fits" quant_bits=4.0 tier.
    repo_id = "TheBloke/OpenLlama-3B-GGUF"
    candidates = ["openllama-3b.Q4_K_S.gguf", "openllama-3b.Q4_K_M.gguf"]

    def fake_resolve(name):
        raise cli.AmbiguousModelError(repo_id, candidates)

    monkeypatch.setattr(cli, "resolve_model", fake_resolve)
    monkeypatch.setattr(cli, "scan_hardware", lambda: _HARDWARE)
    monkeypatch.setattr(
        cli.predictor, "load_cached_model", lambda: {"trees": ["stub-tree"]}
    )

    def fake_predict_speed(trees, hw, candidate):
        # Q4_K_M "wins" its tier, Q4_K_S loses it.
        return 12.0 if candidate["filename"] == "openllama-3b.Q4_K_M.gguf" else 5.0

    monkeypatch.setattr(cli.predictor, "predict_speed", fake_predict_speed)

    captured_choices = {}

    def fake_select(message, choices):
        captured_choices["choices"] = choices
        return None

    monkeypatch.setattr(cli.questionary, "select", fake_select)
    monkeypatch.setattr(cli, "_ask_select", lambda question: None)

    result = runner.invoke(cli.app, ["install", repo_id])

    assert result.exit_code == 0
    titles_by_filename = {c.value: c.title for c in captured_choices["choices"]}
    fast_title = titles_by_filename["openllama-3b.Q4_K_M.gguf"]
    slow_title = titles_by_filename["openllama-3b.Q4_K_S.gguf"]
    assert fast_title == [
        (
            "fg:green bold",
            "openllama-3b.Q4_K_M.gguf  (✓ fits, ~1.8GB needed, predicted fastest)",
        )
    ]
    assert isinstance(slow_title, str)
    assert "predicted fastest" not in slow_title


def test_quant_picker_no_green_marks_when_predictor_model_uncached(isolated_omm_home, monkeypatch):
    repo_id = "TheBloke/Llama-2-7B-GGUF"
    candidates = ["llama-2-7b.Q4_K_M.gguf"]

    def fake_resolve(name):
        raise cli.AmbiguousModelError(repo_id, candidates)

    monkeypatch.setattr(cli, "resolve_model", fake_resolve)
    monkeypatch.setattr(cli, "scan_hardware", lambda: _HARDWARE)
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)

    captured_choices = {}

    def fake_select(message, choices):
        captured_choices["choices"] = choices
        return None

    monkeypatch.setattr(cli.questionary, "select", fake_select)
    monkeypatch.setattr(cli, "_ask_select", lambda question: None)

    result = runner.invoke(cli.app, ["install", repo_id])

    assert result.exit_code == 0
    (choice,) = captured_choices["choices"]
    assert isinstance(choice.title, str)
    assert "predicted fastest" not in choice.title
```

Note: both candidates are Q4 tier (`Q4_K_S`/`Q4_K_M` both parse to `quant_bits=4.0`), so they land in the same tier and only one gets the green mark. See the in-test comment for the GB budget math.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_cli_install_quant_picker.py -v -k predicted_fastest_variant_green or predictor_model_uncached`
Expected: FAIL (green title not present / attribute errors — `_predicted_fastest_filenames` doesn't exist yet)

- [ ] **Step 3: Implement the helper + wire it into `_pick_quant_variant`**

In `src/omm/cli.py`, change the import at line 52:

```python
from omm.hardware import HardwareInfo, calculate_memory_budget, scan_hardware
```

Change the `hub` import block (lines 54-63) to also pull in the new function:

```python
from omm.hub import (
    HF_DOWNLOAD,
    AmbiguousModelError,
    ModelResolutionError,
    QuantVariant,
    ResolvedModel,
    best_filenames_by_tier,
    rank_quant_variants,
    remote_file_size,
    remote_file_sha256,
    resolve_model,
)
```

Add this new helper directly above `_pick_quant_variant` (before line 718):

```python
def _predicted_fastest_filenames(
    variants: list[QuantVariant], repo_id: str | None, hw: HardwareInfo
) -> set[str]:
    """Filenames that are the fastest-predicted variant in their quant-bits
    tier, per the cached ML speed model. Empty when no model is cached, so
    callers fall back to plain (uncolored) rendering."""
    artifact = predictor.load_cached_model()
    trees = artifact.get("trees") if artifact else None
    if trees is None:
        return set()

    predicted_speed = {}
    for variant in variants:
        if variant.fits is not True:
            continue
        candidate = {"repo_id": repo_id, "filename": variant.filename}
        speed = predictor.predict_speed(trees, hw, candidate)
        if speed > 0:
            predicted_speed[variant.filename] = speed

    return best_filenames_by_tier(variants, predicted_speed)
```

Replace the body of `_pick_quant_variant` (`src/omm/cli.py:718-760`) with:

```python
def _pick_quant_variant(error: AmbiguousModelError) -> str | None:
    """Rank the ambiguous repo's .gguf files by fit against this PC's RAM/VRAM
    and let the user pick one, cursor defaulted to the best-fitting, highest
    quality option. The predicted-fastest variant in each quant-bits tier is
    highlighted in green, per the cached ML speed model (skipped entirely if
    no model is cached)."""
    info = scan_hardware()
    available_gb = calculate_memory_budget(info).model_budget_gb

    variants = rank_quant_variants(error.candidates, available_gb, error.param_count_b)
    resolved_variants = []
    for variant in variants:
        if variant.required_gb is not None:
            resolved_variants.append(variant)
            continue
        size_bytes = remote_file_size(error.repo_id, variant.filename)
        if size_bytes is None:
            resolved_variants.append(variant)
            continue
        required_gb = size_bytes / (1024**3) * 1.2
        resolved_variants.append(
            type(variant)(
                filename=variant.filename,
                quant_bits=variant.quant_bits,
                required_gb=required_gb,
                fits=required_gb <= available_gb,
            )
        )
    variants = sorted(
        resolved_variants,
        key=lambda variant: (variant.fits is not True, -(variant.quant_bits or 0)),
    )

    fastest_filenames = _predicted_fastest_filenames(variants, error.repo_id, info)

    choices = []
    for v in variants:
        if v.fits is True:
            note = f"✓ fits, ~{v.required_gb:.1f}GB needed"
        elif v.fits is False:
            note = f"may not fit, ~{v.required_gb:.1f}GB needed (you have {available_gb:.1f}GB)"
        else:
            note = "fit unknown"
        if v.filename in fastest_filenames:
            title = [("fg:green bold", f"{v.filename}  ({note}, predicted fastest)")]
        else:
            title = f"{v.filename}  ({note})"
        choices.append(questionary.Choice(title=title, value=v.filename))

    return _ask_select(
        questionary.select(f"Select a quantization variant for '{error.repo_id}':", choices=choices)
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_cli_install_quant_picker.py -v`
Expected: 4 passed

- [ ] **Step 5: Run the full test suite**

Run: `.venv/bin/pytest tests/ -q`
Expected: all pass (no regressions in `test_hub_quant_variants.py`, `test_cli_search.py`, `test_cli_hardware_fit.py`, etc.)

- [ ] **Step 6: Commit**

```bash
git add src/omm/cli.py tests/test_cli_install_quant_picker.py
git commit -m "feat: highlight predicted-fastest quant variant per tier in green"
```

---

## Post-Plan Verification

- [ ] Manually sanity-check by running `omm search deepseek` then `omm install <a multi-quant result>` against a repo with several same-tier quant files, confirming exactly one green line per tier and no crash when `~/.omm/recommend_model.json` (or equivalent cache path in `RECOMMEND_MODEL_PATH`) is absent.
