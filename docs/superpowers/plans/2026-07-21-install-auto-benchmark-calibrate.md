# Install Auto Benchmark+Calibrate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `omm install` runs benchmark and local calibration automatically with no prompt; the only confirm left is whether to upload the benchmark result. `omm calibrate` moves under `omm setting calibrate`.

**Architecture:** Single-file change in `src/omm/cli.py`. Split the existing combined "benchmark+upload" confirm in `_install_impl` into an unconditional benchmark step, a new silent local-calibration helper (`_maybe_auto_calibrate`), and an upload-only confirm. Move `calibrate` from a top-level `@app.command()` to `@setting_app.command(name="calibrate")` and add it to the `setting_menu` interactive picker.

**Tech Stack:** Python, Typer, questionary, pytest (existing stack, no new deps).

## Global Constraints

- All user-facing CLI strings stay in English (existing convention, confirmed by user).
- No backward-compat alias for the moved `calibrate` command — full move, per existing project convention of deleting rather than aliasing.
- `omm contribute`'s unattended upload behavior (no per-model confirm) must not regress.
- Local calibration must be best-effort: never raise, never block install, silent no-op if no cached recommendation model.

---

### Task 1: Move `calibrate` under `omm setting`

**Files:**
- Modify: `src/omm/cli.py:1186-1247` (the `calibrate` function + its `@app.command()` decorator)
- Modify: `src/omm/cli.py:1302-1359` (`setting_menu`)
- Modify: `tests/test_cli_calibrate_local.py:28`

**Interfaces:**
- Consumes: existing `calibrate(model_name: str | None) -> None` body — unchanged.
- Produces: `calibrate` becomes reachable as `omm setting calibrate` and as `setting_menu`'s "Calibrate" choice. No other task depends on this one.

- [ ] **Step 1: Update the test to invoke the new command path first (red)**

Edit `tests/test_cli_calibrate_local.py:28`:

```python
    result = runner.invoke(cli.app, ["setting", "calibrate", filename])
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `.venv/bin/pytest tests/test_cli_calibrate_local.py -v`
Expected: FAIL — `omm setting calibrate` doesn't exist yet (Typer reports unknown command/usage error), `result.exit_code != 0`.

- [ ] **Step 3: Move the decorator**

In `src/omm/cli.py`, change:

```python
@app.command()
def calibrate(
```

to:

```python
@setting_app.command(name="calibrate")
def calibrate(
```

Leave the rest of the function body (lines 1188-1247) exactly as-is.

- [ ] **Step 4: Add "Calibrate" to the `setting_menu` picker**

In `src/omm/cli.py`, inside `setting_menu` (around line 1307-1319), add a choice:

```python
                choices=[
                    questionary.Choice("UI mode", value="ui"),
                    questionary.Choice("Telemetry", value="telemetry"),
                    questionary.Choice("Calibrate", value="calibrate"),
                    questionary.Choice("Catalog trust", value="catalog-trust"),
                    questionary.Choice("Catalog status", value="catalog-status"),
                    questionary.Choice("Catalog rollback", value="catalog-rollback"),
                ],
```

Then add a branch (after the `elif choice == "telemetry":` block, around line 1349, before `elif choice == "catalog-trust":`):

```python
        elif choice == "calibrate":
            model_name = questionary.text(
                "Model to calibrate (blank for smallest installed):"
            ).ask()
            calibrate(model_name or None)
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/test_cli_calibrate_local.py -v`
Expected: PASS

- [ ] **Step 6: Run the full test suite to check nothing else referenced the old top-level `calibrate` command**

Run: `.venv/bin/pytest tests/ -k calibrate -v`
Expected: PASS, no other test invokes `["calibrate", ...]` at the top level.

- [ ] **Step 7: Commit**

```bash
git add src/omm/cli.py tests/test_cli_calibrate_local.py
git commit -m "feat: move omm calibrate under omm setting calibrate"
```

---

### Task 2: Split `_install_impl` — auto benchmark+calibrate, confirm only before upload

**Files:**
- Modify: `src/omm/cli.py:737-831` (`_install_impl`)
- Modify: `src/omm/cli.py:1752-1754` (`_run_contribution_loop`'s call site)
- Test: `tests/test_install_impl.py`
- Test: `tests/test_cli_install_confirm.py`

**Interfaces:**
- Consumes: `predictor.load_cached_model()`, `predictor.predict_speed_interval(trees, hardware, candidate, engine="ollama", apply_calibration=False) -> tuple[float, float, float]`, `calibration.record_calibration(hardware, *, measured_tokens_per_sec, predicted_tokens_per_sec, engine="ollama") -> float` (all pre-existing, used identically to the `calibrate` command in Task 1).
- Produces: `_install_impl(resolved, *, auto_upload: bool = False, skip_unfit: bool = False, stop_event=None) -> InstallOutcome` — note the renamed keyword `auto_upload` (was `auto_benchmark`). New private helper `_maybe_auto_calibrate(filename: str, repo_id: str | None, dest: Path, tokens_per_sec: float) -> None`, no return value, never raises.

- [ ] **Step 1: Write the failing tests (red)**

Replace the two `auto_benchmark=True` call sites in `tests/test_install_impl.py` with `auto_upload=True`:

`test_auto_benchmark_skips_confirm_prompt_and_sends_telemetry` (rename to `test_auto_upload_skips_confirm_prompt_and_sends_telemetry`):

```python
def test_auto_upload_skips_confirm_prompt_and_sends_telemetry(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    monkeypatch.setattr(cli, "download_file", lambda url, dest: dest.write_bytes(b"x"))
    _stub_common(monkeypatch)
    monkeypatch.setattr(
        cli, "_ask_confirm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no prompt"))
    )
    monkeypatch.setattr(cli.benchmark, "benchmark_ollama", lambda tag: 55.0)
    monkeypatch.setattr(cli.telemetry, "send_event", lambda event, force=False: True)

    outcome = cli._install_impl(_resolved(), auto_upload=True)

    assert outcome.tokens_per_sec == 55.0
    assert outcome.telemetry_sent is True
```

`test_stop_event_set_during_benchmark_raises_contribution_stopped` — drop the now-unnecessary `auto_benchmark=True` kwarg (benchmark always runs):

```python
def test_stop_event_set_during_benchmark_raises_contribution_stopped(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    monkeypatch.setattr(
        cli, "download_file", lambda url, dest, stop_check=None: dest.write_bytes(b"x")
    )
    _stub_common(monkeypatch)

    def slow_benchmark(tag):
        time.sleep(2)
        return 10.0

    monkeypatch.setattr(cli.benchmark, "benchmark_ollama", slow_benchmark)
    stop_event = threading.Event()
    threading.Timer(0.05, stop_event.set).start()

    with pytest.raises(cli.ContributionStopped):
        cli._install_impl(_resolved(), stop_event=stop_event)
```

Add a new test for the split confirm behavior, appended to `tests/test_install_impl.py`:

```python
def test_benchmark_always_runs_but_upload_needs_confirm(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    monkeypatch.setattr(cli, "download_file", lambda url, dest: dest.write_bytes(b"x"))
    _stub_common(monkeypatch)
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: False)
    bench_calls = []
    monkeypatch.setattr(
        cli.benchmark, "benchmark_ollama", lambda tag: bench_calls.append(tag) or 42.0
    )
    sent = []
    monkeypatch.setattr(
        cli.telemetry, "send_event", lambda event, force=False: sent.append((event, force))
    )

    outcome = cli._install_impl(_resolved())

    assert bench_calls == ["tinyllama"]
    assert outcome.tokens_per_sec == 42.0
    assert sent == []
    assert outcome.telemetry_sent is False


def test_auto_calibrate_runs_silently_when_cached_model_available(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(
        cli.predictor, "load_cached_model", lambda: {"trees": [{"leaf": True, "value": 20.0}]}
    )
    monkeypatch.setattr(cli, "scan_hardware", lambda: object())
    monkeypatch.setattr(
        cli.predictor,
        "predict_speed_interval",
        lambda *args, **kwargs: (20.0, 20.0, 20.0),
    )
    monkeypatch.setattr(cli, "download_file", lambda url, dest: dest.write_bytes(b"x"))
    _stub_common(monkeypatch)
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: True)
    monkeypatch.setattr(cli.benchmark, "benchmark_ollama", lambda tag: 30.0)
    monkeypatch.setattr(cli.telemetry, "send_event", lambda event, force=False: True)
    recorded = {}
    monkeypatch.setattr(
        cli.calibration,
        "record_calibration",
        lambda hardware, **kwargs: recorded.update(kwargs) or 1.5,
    )

    cli._install_impl(_resolved())

    assert recorded["measured_tokens_per_sec"] == 30.0
    assert recorded["predicted_tokens_per_sec"] == 20.0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_install_impl.py -v`
Expected: FAIL — `_install_impl() got an unexpected keyword argument 'auto_upload'` for the renamed tests; the new confirm-split test fails because `benchmark_ollama` currently isn't called when `_ask_confirm` returns `False`; the calibrate test fails because `cli.calibration` isn't imported/used in `_install_impl` yet (or `record_calibration` is never called).

- [ ] **Step 3: Confirm `calibration` module is imported in `cli.py`**

Run: `grep -n "^from omm import\|^import\|calibration" src/omm/cli.py | grep calibration`

If `calibration` is not already imported (it's used by the moved `calibrate` command via `calibration.record_calibration`, so it should already be imported — verify, don't add a duplicate import).

- [ ] **Step 4: Implement `_maybe_auto_calibrate`**

In `src/omm/cli.py`, add this helper directly above `_install_impl` (before line 737):

```python
def _maybe_auto_calibrate(
    filename: str, repo_id: str | None, dest: Path, tokens_per_sec: float
) -> None:
    """Best-effort local calibration right after a successful benchmark.
    Silent no-op if there's no cached model to compare against - this must
    never block or fail the install."""
    artifact = predictor.load_cached_model()
    if not artifact or not artifact.get("trees"):
        return
    hardware = scan_hardware()
    candidate = {
        "repo_id": repo_id,
        "filename": filename,
        "size_bytes": dest.stat().st_size if dest.exists() else None,
    }
    try:
        predicted, _, _ = predictor.predict_speed_interval(
            artifact["trees"],
            hardware,
            candidate,
            engine="ollama",
            apply_calibration=False,
        )
    except (ValueError, KeyError, TypeError, IndexError):
        return
    if predicted <= 0:
        return
    factor = calibration.record_calibration(
        hardware,
        measured_tokens_per_sec=tokens_per_sec,
        predicted_tokens_per_sec=predicted,
        engine="ollama",
    )
    console.print(
        f"[dim]Local calibration updated: correction ×{factor:.2f} "
        "(not uploaded).[/dim]"
    )
```

- [ ] **Step 5: Replace the confirm block in `_install_impl`**

In `src/omm/cli.py`, change the signature:

```python
def _install_impl(
    resolved,
    *,
    auto_benchmark: bool = False,
    skip_unfit: bool = False,
    stop_event: threading.Event | None = None,
) -> InstallOutcome:
```

to:

```python
def _install_impl(
    resolved,
    *,
    auto_upload: bool = False,
    skip_unfit: bool = False,
    stop_event: threading.Event | None = None,
) -> InstallOutcome:
```

And replace this block (currently lines 807-827):

```python
    tokens_per_sec = None
    telemetry_sent = False
    if linked["ollama"]:
        should_benchmark = auto_benchmark or _ask_confirm(
            "Benchmark this model's speed and send the result to the server?"
        )
        if should_benchmark:
            console.print("Benchmarking...")
            try:
                tokens_per_sec = _run_interruptible(
                    lambda: benchmark.benchmark_ollama(ollama_tag), stop_event
                )
            except _Interrupted as e:
                raise ContributionStopped(filename) from e
            if tokens_per_sec:
                console.print(f"[cyan]{tokens_per_sec:.1f} tok/s[/cyan]")
            telemetry_sent = _report_telemetry(filename, repo_id, tokens_per_sec)
        else:
            telemetry.log_attempt("declined_by_user", filename)
    else:
        telemetry.log_attempt("not_attempted_no_ollama_link", filename)
```

with:

```python
    tokens_per_sec = None
    telemetry_sent = False
    if linked["ollama"]:
        console.print("Benchmarking...")
        try:
            tokens_per_sec = _run_interruptible(
                lambda: benchmark.benchmark_ollama(ollama_tag), stop_event
            )
        except _Interrupted as e:
            raise ContributionStopped(filename) from e

        if tokens_per_sec:
            console.print(f"[cyan]{tokens_per_sec:.1f} tok/s[/cyan]")
            _maybe_auto_calibrate(filename, repo_id, dest, tokens_per_sec)

            want_upload = auto_upload or _ask_confirm(
                "Send this machine's benchmark result to the server?"
            )
            if want_upload:
                telemetry_sent = _report_telemetry(filename, repo_id, tokens_per_sec)
            else:
                telemetry.log_attempt("declined_by_user", filename)
        else:
            telemetry_sent = _report_telemetry(filename, repo_id, tokens_per_sec)
    else:
        telemetry.log_attempt("not_attempted_no_ollama_link", filename)
```

- [ ] **Step 6: Update the `omm contribute` call site**

In `src/omm/cli.py`, inside `_run_contribution_loop` (around line 1752-1754), change:

```python
            outcome = _install_impl(
                resolved, auto_benchmark=True, skip_unfit=True, stop_event=stop_event
            )
```

to:

```python
            outcome = _install_impl(
                resolved, auto_upload=True, skip_unfit=True, stop_event=stop_event
            )
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_install_impl.py -v`
Expected: PASS (all tests including the two new ones)

- [ ] **Step 8: Update `tests/test_cli_install_confirm.py`**

Replace `test_install_skips_benchmark_and_telemetry_on_no` (benchmark can no longer be skipped) with a test for upload-only skipping:

```python
def test_install_runs_benchmark_but_skips_upload_on_no(isolated_omm_home, monkeypatch):
    _stub_successful_install(monkeypatch, isolated_omm_home)
    monkeypatch.setattr(cli, "_ask_confirm", lambda message, default=False: False)
    bench_calls = []
    monkeypatch.setattr(cli.benchmark, "benchmark_ollama", lambda tag: bench_calls.append(tag) or 42.0)
    sent = []
    monkeypatch.setattr(cli.telemetry, "send_event", lambda event, force=False: sent.append((event, force)))

    result = runner.invoke(cli.app, ["install", "tinyllama-1.1b-q4"])

    assert result.exit_code == 0, result.stdout
    assert bench_calls == ["tinyllama"]
    assert sent == []
```

Leave `test_install_runs_benchmark_and_telemetry_on_yes` and `test_ask_confirm_uses_questionary_with_auto_enter` unchanged — both still hold under the new behavior.

- [ ] **Step 9: Run the full test file**

Run: `.venv/bin/pytest tests/test_cli_install_confirm.py -v`
Expected: PASS

- [ ] **Step 10: Run the full suite**

Run: `.venv/bin/pytest tests/ -v`
Expected: PASS. Pay particular attention to `tests/test_contribute_loop.py` and `tests/test_cli_telemetry_reliability.py` — grep them first for any other `auto_benchmark=` usage:

Run: `grep -rn "auto_benchmark" tests/ src/`
Expected: no remaining matches (all renamed to `auto_upload`).

- [ ] **Step 11: Commit**

```bash
git add src/omm/cli.py tests/test_install_impl.py tests/test_cli_install_confirm.py
git commit -m "feat: auto-run benchmark+calibrate on install, confirm only before upload"
```

---

## Self-Review Notes

- **Spec coverage:** Task 1 covers spec section 1 (calibrate move + menu). Task 2 covers spec sections 2, 3, 5 (confirm split, auto-calibrate helper, tests). Spec section 4 (`install()` command output) required no code change, confirmed no task needed. Spec section 6 (unaffected areas) requires no task by definition.
- **Placeholder scan:** none found — every step has literal code.
- **Type consistency:** `_maybe_auto_calibrate(filename: str, repo_id: str | None, dest: Path, tokens_per_sec: float) -> None` signature matches its one call site in Task 2 Step 5. `_install_impl`'s `auto_upload` parameter name matches both call sites (plain `install()` uses the default `False`; `_run_contribution_loop` passes `True`).
