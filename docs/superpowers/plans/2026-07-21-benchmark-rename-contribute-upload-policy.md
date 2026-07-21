# `omm benchmark` rename + contribute quality upload + tri-state policy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rename `omm quality-eval` to `omm benchmark`, make `omm contribute` benchmark candidates with the quality+speed evaluator instead of the single-shot probe (uploading accuracy alongside speed), let standalone `omm benchmark` optionally upload its results, and replace the dead `telemetry_opt_in` bool with a real `ask`/`always`/`never` upload policy shared by `omm install`, `omm benchmark`, and `omm contribute`.

**Architecture:** All changes live in `src/omm/cli.py`, `src/omm/config.py`, `src/omm/telemetry.py`, and `src/localfit_server/app.py`. No new modules. `_install_impl` gains one new optional branch (`use_quality_eval`) rather than being split into new functions, so the five existing `test_contribute_loop.py` tests (which stub `cli._install_impl` wholesale) keep passing unchanged. A single helper `_resolve_upload_decision` centralizes the ask/always/never check and is used by `omm install`, `omm benchmark`, and (indirectly, via the command-level gate) `omm contribute`.

**Tech Stack:** Python, Typer, pytest, FastAPI/Pydantic (server), no new dependencies.

## Global Constraints

- No backward-compat shims: `telemetry_opt_in` is removed from `DEFAULT_CONFIG`, but existing `~/.omm/config.json` files with that key must still carry the user's intent forward (`True` → `"always"`, `False`/absent → `"ask"`) via a one-time migration in `_merge_config`.
- `omm install`'s benchmark behavior must not change (still single-shot `benchmark.benchmark_ollama`, still confirms before upload under the default `"ask"` policy).
- Server schema additions (`quality_pack_id`, `quality_pack_version`, `quality_correct`, `quality_total`, `quality_accuracy`) are optional and all-or-nothing; no new SQL columns in `db.py` (they ride along in `event_json`).
- Category-level quality breakdown is out of scope — only totals (`correct`/`total`/`accuracy`) leave the machine.
- Spec: `docs/superpowers/specs/2026-07-21-benchmark-rename-contribute-upload-policy-design.md`. Note one deviation from the spec's section 3 sketch: instead of splitting `_install_impl` into `_download_and_link` + a new contribute-only function, this plan adds a single `use_quality_eval` branch parameter to the existing `_install_impl`, matching its established pattern (`auto_upload`, `skip_unfit`) and avoiding a rewrite of `tests/test_contribute_loop.py`'s five existing tests.

---

### Task 1: Tri-state telemetry policy in config

**Files:**
- Modify: `src/omm/config.py:24-55` (`DEFAULT_CONFIG`, `_merge_config`)
- Test: `tests/test_config_migration.py`

**Interfaces:**
- Produces: `DEFAULT_CONFIG["telemetry_send_policy"] == "ask"`, `DEFAULT_CONFIG["contribute_always_ack"] == False`. `load_config()["telemetry_send_policy"]` is always one of `"ask"`/`"always"`/`"never"` and `telemetry_opt_in` never appears in the returned dict.

- [ ] **Step 1: Write the failing migration test**

Add to `tests/test_config_migration.py` (near the existing `telemetry_opt_in` + legacy-firebase tests):

```python
def test_telemetry_opt_in_true_migrates_to_always_policy(isolated_omm_home):
    config.CONFIG_PATH.write_text(json.dumps({"telemetry_opt_in": True}))

    loaded = config.load_config()

    assert loaded["telemetry_send_policy"] == "always"
    assert "telemetry_opt_in" not in loaded


def test_telemetry_opt_in_false_migrates_to_ask_policy(isolated_omm_home):
    config.CONFIG_PATH.write_text(json.dumps({"telemetry_opt_in": False}))

    loaded = config.load_config()

    assert loaded["telemetry_send_policy"] == "ask"
    assert "telemetry_opt_in" not in loaded


def test_fresh_config_defaults_to_ask_policy(isolated_omm_home):
    loaded = config.load_config()

    assert loaded["telemetry_send_policy"] == "ask"
    assert loaded["contribute_always_ack"] is False
```

(Check the top of `tests/test_config_migration.py` already imports `json` and `config`; add `import json` if missing.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_config_migration.py -v -k policy`
Expected: FAIL — `KeyError: 'telemetry_send_policy'` or similar (field doesn't exist yet).

- [ ] **Step 3: Implement the migration**

Replace `src/omm/config.py:24-55` with:

```python
DEFAULT_CONFIG: dict[str, Any] = {
    "telemetry_send_policy": "ask",
    # Local-only by default. Teams may configure the bundled FastAPI server;
    # Firebase remains an explicit legacy compatibility option.
    "telemetry_endpoint": None,
    "telemetry_backend": "local",
    "rules_url": None,
    "model_url": "https://raw.githubusercontent.com/minigu5/Localfit/main/published/recommend-model.json",
    "default_engine": None,
    "external_scan_done": False,
    "catalog_manifest_url": None,
    "catalog_public_key": None,
    "ui_mode": "compact",
    "contribute_always_ack": False,
}


def ensure_omm_home() -> None:
    MODELS_DIR.mkdir(parents=True, exist_ok=True)


def _merge_config(data: dict[str, Any]) -> dict[str, Any]:
    if "telemetry_send_policy" not in data and "telemetry_opt_in" in data:
        data = {
            **data,
            "telemetry_send_policy": "always" if data["telemetry_opt_in"] else "ask",
        }
    merged = {**DEFAULT_CONFIG, **data}
    merged.pop("telemetry_opt_in", None)
    if "telemetry_backend" not in data:
        endpoint = data.get("telemetry_endpoint")
        if endpoint == LEGACY_FIREBASE_ENDPOINT and merged.get("telemetry_send_policy") != "always":
            merged["telemetry_endpoint"] = None
            merged["telemetry_backend"] = "local"
        elif isinstance(endpoint, str) and "firebaseio.com" in endpoint:
            merged["telemetry_backend"] = "firebase_legacy"
        elif endpoint:
            merged["telemetry_backend"] = "self_hosted"
    return merged
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_config_migration.py tests/test_atomic_writes.py -v`
Expected: the 3 new tests PASS. `tests/test_atomic_writes.py:51` (`config.load_config()["telemetry_opt_in"] is False`) will now FAIL — fix it in this same step by changing that line to `assert config.load_config()["telemetry_send_policy"] == "ask"`.

- [ ] **Step 5: Commit**

```bash
git add src/omm/config.py tests/test_config_migration.py tests/test_atomic_writes.py
git commit -m "feat: replace dead telemetry_opt_in with ask/always/never policy"
```

---

### Task 2: `telemetry.send_event` gate uses the new policy

**Files:**
- Modify: `src/omm/telemetry.py:100-104`
- Test: `tests/test_telemetry.py`

**Interfaces:**
- Consumes: `load_config()["telemetry_send_policy"]` (Task 1).
- Produces: `send_event(event, force=False)` behavior unchanged in effect (still gates on "not explicitly enabled"), just reads the new field.

- [ ] **Step 1: Update the fixtures (tests first)**

In `tests/test_telemetry.py`, replace every `"telemetry_opt_in": False` with `"telemetry_send_policy": "ask"` and every `"telemetry_opt_in": True` with `"telemetry_send_policy": "always"` (7 occurrences total, at lines 15, 30, 45, 60, 75, 90, 111 — replace all).

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_telemetry.py -v`
Expected: FAIL — with the old `telemetry.py` code, `config_data.get("telemetry_opt_in")` is `None` for these fixtures (key doesn't exist), so `not force and not None` (`not None` is `True`) still skips exactly like before by coincidence for the `"ask"`-renamed-False cases, but the `"always"`-renamed-True cases now also skip (since `telemetry_opt_in` key is simply absent) — those tests expecting a real send will FAIL.

- [ ] **Step 3: Implement**

Replace `src/omm/telemetry.py:100-104`:

```python
def send_event(event: dict[str, Any], force: bool = False) -> bool:
    config_data = load_config()
    if not force and config_data.get("telemetry_send_policy") != "always":
        log_attempt("skipped_opt_out")
        return False
```

(rest of the function unchanged)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_telemetry.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/omm/telemetry.py tests/test_telemetry.py
git commit -m "feat: gate send_event on telemetry_send_policy instead of opt_in bool"
```

---

### Task 3: `omm setting telemetry --ask` + menu update

**Files:**
- Modify: `src/omm/cli.py:1183-1229` (`configure_telemetry`), `src/omm/cli.py:1375-1394` (`setting_menu` telemetry branch)
- Test: `tests/test_cli_setting.py`, `tests/test_cli_telemetry_config.py`

**Interfaces:**
- Produces: `omm setting telemetry --enable` sets `telemetry_send_policy="always"`, `--disable` sets `"never"`, `--ask` sets `"ask"`. Passing more than one of the three is an error (exit 1).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_cli_telemetry_config.py`:

```python
def test_telemetry_ask_resets_policy_to_ask(isolated_omm_home):
    config.update_config(telemetry_send_policy="always", telemetry_endpoint="https://example.com")

    result = runner.invoke(cli.app, ["setting", "telemetry", "--ask"])

    assert result.exit_code == 0, result.stdout
    assert config.load_config()["telemetry_send_policy"] == "ask"


def test_telemetry_disable_sets_never_policy(isolated_omm_home):
    result = runner.invoke(cli.app, ["setting", "telemetry", "--disable"])

    assert result.exit_code == 0, result.stdout
    assert config.load_config()["telemetry_send_policy"] == "never"


def test_telemetry_rejects_multiple_policy_flags(isolated_omm_home):
    result = runner.invoke(cli.app, ["setting", "telemetry", "--enable", "--disable"])

    assert result.exit_code == 1
    assert "only one" in result.stdout.lower()
```

Update the two existing assertions that check `telemetry_opt_in`:
- `tests/test_cli_setting.py:40`: `assert config.load_config()["telemetry_opt_in"] is False` → `assert config.load_config()["telemetry_send_policy"] == "ask"`
- `tests/test_cli_setting.py:58`: `assert saved["telemetry_opt_in"] is True` → `assert saved["telemetry_send_policy"] == "always"`

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cli_setting.py tests/test_cli_telemetry_config.py -v`
Expected: FAIL — `--ask` is not a recognized option yet (Typer "no such option" error), and the two updated assertions fail against current `telemetry_opt_in`-based code.

- [ ] **Step 3: Implement `configure_telemetry`**

Replace `src/omm/cli.py:1183-1228`:

```python
@setting_app.command(name="telemetry")
def configure_telemetry(
    endpoint: str = typer.Option(
        None,
        "--endpoint",
        help="Self-hosted HTTPS endpoint, localhost URL, or 'none' to clear it.",
    ),
    enable: bool = typer.Option(False, "--enable", help="Always send benchmark results without asking."),
    disable: bool = typer.Option(False, "--disable", help="Never send benchmark results."),
    ask: bool = typer.Option(False, "--ask", help="Ask every time before sending (default)."),
) -> None:
    """Configure optional uploads; the default remains asking every time."""
    chosen = [flag for flag in (enable, disable, ask) if flag]
    if len(chosen) > 1:
        console.print("[red]Choose only one of --enable, --disable, or --ask.[/red]")
        raise typer.Exit(1)
    current = load_config()
    changes = {}
    if endpoint is not None:
        if endpoint.lower() == "none":
            changes.update(telemetry_endpoint=None, telemetry_backend="local")
        elif not telemetry.secure_endpoint(endpoint):
            console.print("[red]Use HTTPS, or HTTP only for localhost.[/red]")
            raise typer.Exit(1)
        else:
            changes.update(
                telemetry_endpoint=endpoint,
                telemetry_backend=(
                    "firebase_legacy" if "firebaseio.com" in endpoint else "self_hosted"
                ),
            )
    prospective_endpoint = changes.get("telemetry_endpoint", current.get("telemetry_endpoint"))
    if enable:
        if not prospective_endpoint:
            console.print("[red]Set --endpoint before enabling uploads.[/red]")
            raise typer.Exit(1)
        changes["telemetry_send_policy"] = "always"
    elif disable:
        changes["telemetry_send_policy"] = "never"
    elif ask:
        changes["telemetry_send_policy"] = "ask"
    if changes:
        current = config_mod.update_config(**changes)
    table = Table(title="Benchmark data policy", show_header=False)
    table.add_column("Field", style="cyan")
    table.add_column("Value")
    policy = current.get("telemetry_send_policy", "ask")
    table.add_row("Uploads", {"always": "always", "never": "never", "ask": "ask (default)"}[policy])
    table.add_row("Backend", str(current.get("telemetry_backend") or "local"))
    table.add_row("Endpoint", str(current.get("telemetry_endpoint") or "not configured"))
    console.print(table)
```

- [ ] **Step 4: Implement the menu update**

Replace `src/omm/cli.py:1375-1394`:

```python
        elif choice == "telemetry":
            endpoint = questionary.text(
                "Endpoint (blank to keep current, 'none' to clear):"
            ).ask()
            action = _ask_select(
                questionary.select(
                    "Uploads:",
                    choices=[
                        questionary.Choice("Always send", value="enable"),
                        questionary.Choice("Never send", value="disable"),
                        questionary.Choice("Ask every time", value="ask"),
                        questionary.Choice("Leave unchanged", value="skip"),
                    ],
                )
            )
            if action is not None:
                configure_telemetry(
                    endpoint=endpoint or None,
                    enable=(action == "enable"),
                    disable=(action == "disable"),
                    ask=(action == "ask"),
                )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_cli_setting.py tests/test_cli_telemetry_config.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/omm/cli.py tests/test_cli_setting.py tests/test_cli_telemetry_config.py
git commit -m "feat: add omm setting telemetry --ask to reset to ask-every-time"
```

---

### Task 4: `_resolve_upload_decision` helper wired into `omm install`

**Files:**
- Modify: `src/omm/cli.py:448` area (add helper after `_ask_confirm`), `src/omm/cli.py:862-864` (`_install_impl`'s upload confirm)
- Test: `tests/test_install_impl.py`

**Interfaces:**
- Produces: `_resolve_upload_decision(prompt: str) -> bool`. Reads `load_config()["telemetry_send_policy"]`; `"always"` → `True` with no prompt, `"never"` → `False` with no prompt, `"ask"` → falls through to `_ask_confirm(prompt)`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_install_impl.py`:

```python
def test_resolve_upload_decision_always_skips_prompt(isolated_omm_home):
    cli.config_mod.update_config(telemetry_send_policy="always")

    assert cli._resolve_upload_decision("prompt") is True


def test_resolve_upload_decision_never_skips_prompt(isolated_omm_home, monkeypatch):
    cli.config_mod.update_config(telemetry_send_policy="never")
    monkeypatch.setattr(
        cli, "_ask_confirm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no prompt"))
    )

    assert cli._resolve_upload_decision("prompt") is False


def test_resolve_upload_decision_ask_falls_back_to_confirm(isolated_omm_home, monkeypatch):
    cli.config_mod.update_config(telemetry_send_policy="ask")
    monkeypatch.setattr(cli, "_ask_confirm", lambda message, **k: message == "prompt")

    assert cli._resolve_upload_decision("prompt") is True
    assert cli._resolve_upload_decision("other") is False


def test_install_auto_uploads_without_confirm_when_policy_always(isolated_omm_home, monkeypatch):
    cli.config_mod.update_config(telemetry_send_policy="always")
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    monkeypatch.setattr(cli, "download_file", lambda url, dest: dest.write_bytes(b"x"))
    _stub_common(monkeypatch)
    monkeypatch.setattr(
        cli, "_ask_confirm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no prompt"))
    )
    monkeypatch.setattr(cli.benchmark, "benchmark_ollama", lambda tag: 42.0)
    sent = []
    monkeypatch.setattr(
        cli.telemetry, "send_event", lambda event, force=False: sent.append(event) or True
    )

    outcome = cli._install_impl(_resolved())

    assert outcome.telemetry_sent is True
    assert sent


def test_install_never_uploads_without_confirm_when_policy_never(isolated_omm_home, monkeypatch):
    cli.config_mod.update_config(telemetry_send_policy="never")
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    monkeypatch.setattr(cli, "download_file", lambda url, dest: dest.write_bytes(b"x"))
    _stub_common(monkeypatch)
    monkeypatch.setattr(
        cli, "_ask_confirm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no prompt"))
    )
    monkeypatch.setattr(cli.benchmark, "benchmark_ollama", lambda tag: 42.0)
    monkeypatch.setattr(
        cli.telemetry, "send_event", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no send"))
    )

    outcome = cli._install_impl(_resolved())

    assert outcome.telemetry_sent is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_install_impl.py -v -k "resolve_upload or policy"`
Expected: FAIL — `AttributeError: module 'omm.cli' has no attribute '_resolve_upload_decision'`.

- [ ] **Step 3: Implement**

After `_ask_confirm` (`src/omm/cli.py:448-455`), add:

```python
def _resolve_upload_decision(prompt: str) -> bool:
    policy = load_config().get("telemetry_send_policy", "ask")
    if policy == "always":
        return True
    if policy == "never":
        return False
    return _ask_confirm(prompt)
```

In `_install_impl` (`src/omm/cli.py:862-864`), replace:

```python
            want_upload = auto_upload or _ask_confirm(
                "Send this machine's benchmark result to the server?"
            )
```

with:

```python
            want_upload = auto_upload or _resolve_upload_decision(
                "Send this machine's benchmark result to the server?"
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_install_impl.py -v`
Expected: all PASS (including the pre-existing tests — default policy is `"ask"` in `isolated_omm_home`, so `_resolve_upload_decision` falls through to `_ask_confirm` exactly as before).

- [ ] **Step 5: Commit**

```bash
git add src/omm/cli.py tests/test_install_impl.py
git commit -m "feat: add _resolve_upload_decision and wire it into omm install"
```

---

### Task 5: Rename `omm quality-eval` → `omm benchmark`

**Files:**
- Modify: `src/omm/cli.py:1621-1622` (command decorator + function name)
- Modify: `README.md:21`, `README.md:49`
- Test: rename `tests/test_cli_quality_eval.py` → `tests/test_cli_benchmark.py`, update the two invocations

**Interfaces:**
- Produces: CLI command `omm benchmark <tags...>` (was `omm quality-eval`). Function `benchmark_cmd` (was `quality_eval_cmd`).

- [ ] **Step 1: Rename the test file and update invocations (test first)**

```bash
git mv tests/test_cli_quality_eval.py tests/test_cli_benchmark.py
```

In `tests/test_cli_benchmark.py`, change both `runner.invoke(cli.app, ["quality-eval", ...])` calls to `runner.invoke(cli.app, ["benchmark", ...])`, and rename `test_quality_eval_stops_when_ollama_is_not_running` to `test_benchmark_stops_when_ollama_is_not_running`. Leave `test_quality_eval_saves_local_report_and_never_uploads` as-is for now — Task 7 replaces it.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cli_benchmark.py -v`
Expected: FAIL — no command named `benchmark` yet (Typer "No such command").

- [ ] **Step 3: Rename the command**

`src/omm/cli.py:1621-1622`:

```python
@app.command(name="benchmark")
def benchmark_cmd(
```

(only the decorator's `name=` and the `def` line change; body untouched for now)

Update `README.md:21` and `README.md:49` from `omm quality-eval` to `omm benchmark`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_cli_benchmark.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/omm/cli.py tests/test_cli_benchmark.py tests/test_cli_quality_eval.py README.md
git commit -m "feat: rename omm quality-eval to omm benchmark"
```

---

### Task 6: Extend `_report_telemetry` with sample/quality kwargs

**Files:**
- Modify: `src/omm/cli.py:1691-1723`
- Test: `tests/test_install_impl.py` (or a focused new test)

**Interfaces:**
- Produces: `_report_telemetry(filename, repo_id, tokens_per_sec, *, size_bytes=None, sample_count=1, speed_min=None, speed_max=None, quality=None) -> bool`. `quality`, when not `None`, is `{"pack_id": str, "pack_version": str|None, "correct": int, "total": int, "accuracy": float}`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_install_impl.py`:

```python
def test_report_telemetry_includes_quality_fields_when_provided(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli, "scan_hardware", lambda: object())
    sent = []
    monkeypatch.setattr(
        cli.telemetry, "send_event", lambda event, force=False: sent.append(event) or True
    )

    cli._report_telemetry(
        "small:latest",
        "org/small",
        42.5,
        size_bytes=123,
        sample_count=3,
        speed_min=40.0,
        speed_max=45.0,
        quality={"pack_id": "pack-1", "pack_version": "1.1.0", "correct": 6, "total": 8, "accuracy": 0.75},
    )

    event = sent[0]
    assert event["model_size_bytes"] == 123
    assert event["sample_count"] == 3
    assert event["tokens_per_sec_min"] == 40.0
    assert event["tokens_per_sec_max"] == 45.0
    assert event["quality_pack_id"] == "pack-1"
    assert event["quality_correct"] == 6
    assert event["quality_total"] == 8
    assert event["quality_accuracy"] == 0.75


def test_report_telemetry_omits_quality_fields_by_default(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli, "scan_hardware", lambda: object())
    sent = []
    monkeypatch.setattr(
        cli.telemetry, "send_event", lambda event, force=False: sent.append(event) or True
    )

    cli._report_telemetry("model.gguf", "org/repo", 10.0)

    assert "quality_pack_id" not in sent[0]
    assert sent[0]["sample_count"] == 1
```

`scan_hardware` here must return something with the attributes `_report_telemetry` reads (`ram_total_gb`, `vram_total_gb`, `unified_memory`, `gpu_tflops`) — reuse the `SimpleNamespace` pattern already used in `test_auto_calibrate_runs_silently_when_cached_model_available` instead of `object()`:

```python
from types import SimpleNamespace
...
    monkeypatch.setattr(
        cli, "scan_hardware",
        lambda: SimpleNamespace(ram_total_gb=16.0, vram_total_gb=None, unified_memory=False, gpu_tflops=None),
    )
```
(use this in both new tests in place of the `object()` placeholder above)

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_install_impl.py -v -k report_telemetry`
Expected: FAIL — `TypeError: _report_telemetry() got an unexpected keyword argument 'size_bytes'`.

- [ ] **Step 3: Implement**

Replace `src/omm/cli.py:1691-1723`:

```python
def _report_telemetry(
    filename: str,
    repo_id: str | None,
    tokens_per_sec: float | None,
    *,
    size_bytes: int | None = None,
    sample_count: int = 1,
    speed_min: float | None = None,
    speed_max: float | None = None,
    quality: dict | None = None,
) -> bool:
    if tokens_per_sec is None:
        # Ollama daemon wasn't reachable - not a real "it doesn't run" signal,
        # so skip rather than polluting the speed-regression training data.
        telemetry.log_attempt("skipped_daemon_unreachable", filename)
        console.print(
            "[dim]Telemetry not sent - Ollama daemon wasn't reachable during benchmark.[/dim]"
        )
        return False
    info = scan_hardware()
    if size_bytes is None:
        model_file = MODELS_DIR / filename
        size_bytes = model_file.stat().st_size if model_file.exists() else None
    event = {
        "ram_gb": round(info.ram_total_gb, 1),
        "vram_gb": round(info.vram_total_gb, 1) if info.vram_total_gb is not None else None,
        "unified_memory": info.unified_memory,
        "gpu_tflops": info.gpu_tflops,
        "model_installed": filename,
        "model_repo_id": repo_id,
        "model_size_bytes": size_bytes,
        "engine": "ollama",
        "benchmark_version": 4,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "tokens_per_sec": round(tokens_per_sec, 2),
        "sample_count": sample_count,
        "tokens_per_sec_min": round(speed_min if speed_min is not None else tokens_per_sec, 2),
        "tokens_per_sec_max": round(speed_max if speed_max is not None else tokens_per_sec, 2),
    }
    if quality is not None:
        event.update(
            quality_pack_id=quality["pack_id"],
            quality_pack_version=quality["pack_version"],
            quality_correct=quality["correct"],
            quality_total=quality["total"],
            quality_accuracy=quality["accuracy"],
        )
    sent = telemetry.send_event(event, force=True)
    if not sent:
        console.print("[dim]Telemetry not sent (will retry next time you run omm).[/dim]")
    return sent
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_install_impl.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/omm/cli.py tests/test_install_impl.py
git commit -m "feat: let _report_telemetry carry sample stats and quality summary"
```

---

### Task 7: `omm benchmark` asks to upload at the end

**Files:**
- Modify: `src/omm/cli.py:1621-1638` (`benchmark_cmd`, end of function)
- Test: `tests/test_cli_benchmark.py`

**Interfaces:**
- Consumes: `_resolve_upload_decision` (Task 4), `_report_telemetry(..., size_bytes=, sample_count=, speed_min=, speed_max=, quality=)` (Task 6), `registry.load_registry()`.
- Produces: after printing the table and local-save message, `benchmark_cmd` calls `_resolve_upload_decision(...)`; on `True`, uploads one `_report_telemetry(...)` call per evaluated model.

- [ ] **Step 1: Write the failing tests**

Replace `test_quality_eval_saves_local_report_and_never_uploads` in `tests/test_cli_benchmark.py` with a fuller fixture (this is the "contract changed" test the spec called out) and three new tests:

```python
def _full_report():
    return {
        "schema_version": 1,
        "pack": {"id": "localfit-gsm8k-bilingual-smoke", "version": "1.1.0"},
        "models": [
            {
                "tag": "small:latest",
                "parameter_size": "1B",
                "quantization_level": "Q4_K_M",
                "size_bytes": 900_000_000,
                "quality": {"correct": 6, "total": 8, "accuracy": 0.75},
                "speed": {
                    "median_tokens_per_sec": 42.5,
                    "samples_tokens_per_sec": [41.0, 42.5, 44.0],
                    "runs": 3,
                },
            }
        ],
    }


def test_benchmark_saves_local_report_and_asks_before_upload(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli.quality_mod, "collect_evidence", lambda *a, **k: _full_report())
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: False)
    sent = []
    monkeypatch.setattr(cli.telemetry, "send_event", lambda event, force=False: sent.append(event) or True)

    result = runner.invoke(cli.app, ["benchmark", "small:latest"])

    assert result.exit_code == 0, result.stdout
    assert "6/8 (75.0%)" in result.stdout
    assert "42.5 tok/s" in result.stdout
    paths = list(config.EVALUATIONS_DIR.glob("quality-*.json"))
    assert len(paths) == 1
    assert sent == []


def test_benchmark_uploads_when_confirmed(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli.quality_mod, "collect_evidence", lambda *a, **k: _full_report())
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: True)
    sent = []
    monkeypatch.setattr(cli.telemetry, "send_event", lambda event, force=False: sent.append(event) or True)

    result = runner.invoke(cli.app, ["benchmark", "small:latest"])

    assert result.exit_code == 0, result.stdout
    assert len(sent) == 1
    event = sent[0]
    assert event["model_installed"] == "small:latest"
    assert event["model_size_bytes"] == 900_000_000
    assert event["sample_count"] == 3
    assert event["tokens_per_sec_min"] == 41.0
    assert event["tokens_per_sec_max"] == 44.0
    assert event["quality_pack_id"] == "localfit-gsm8k-bilingual-smoke"
    assert event["quality_correct"] == 6
    assert event["quality_total"] == 8
    assert event["quality_accuracy"] == 0.75


def test_benchmark_never_uploads_when_policy_never(isolated_omm_home, monkeypatch):
    config.update_config(telemetry_send_policy="never")
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli.quality_mod, "collect_evidence", lambda *a, **k: _full_report())
    monkeypatch.setattr(
        cli, "_ask_confirm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no prompt"))
    )
    sent = []
    monkeypatch.setattr(cli.telemetry, "send_event", lambda event, force=False: sent.append(event) or True)

    result = runner.invoke(cli.app, ["benchmark", "small:latest"])

    assert result.exit_code == 0, result.stdout
    assert sent == []


def test_benchmark_uploads_without_confirm_when_policy_always(isolated_omm_home, monkeypatch):
    config.update_config(telemetry_send_policy="always")
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli.quality_mod, "collect_evidence", lambda *a, **k: _full_report())
    monkeypatch.setattr(
        cli, "_ask_confirm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no prompt"))
    )
    sent = []
    monkeypatch.setattr(cli.telemetry, "send_event", lambda event, force=False: sent.append(event) or True)

    result = runner.invoke(cli.app, ["benchmark", "small:latest"])

    assert result.exit_code == 0, result.stdout
    assert len(sent) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cli_benchmark.py -v`
Expected: FAIL — no upload prompt exists yet, so `sent` stays empty in the "uploads" tests.

- [ ] **Step 3: Implement**

Replace `src/omm/cli.py:1630-1637` (the tail of `benchmark_cmd`, from `console.print(table)` through the `json_output` block):

```python
    console.print(table)
    console.print(f"[green]Saved reproducible local evidence to {output}.[/green]")
    console.print(
        "[dim]No generated text or raw hardware names are ever stored. Only the "
        "aggregate numbers below may be shared if you confirm. This small smoke "
        "pack is not a leaderboard.[/dim]"
    )
    if _resolve_upload_decision(
        "Send these benchmark results to the server to help train the recommendation model?"
    ):
        registry_entries = registry.load_registry()
        for model in report["models"]:
            entry = next(
                (e for e in registry_entries.values() if e.get("ollama_name") == model["tag"]),
                None,
            )
            samples = model["speed"]["samples_tokens_per_sec"]
            _report_telemetry(
                model["tag"],
                entry.get("repo_id") if entry else None,
                model["speed"]["median_tokens_per_sec"],
                size_bytes=model.get("size_bytes"),
                sample_count=model["speed"]["runs"],
                speed_min=min(samples),
                speed_max=max(samples),
                quality={
                    "pack_id": report["pack"]["id"],
                    "pack_version": report["pack"]["version"],
                    "correct": model["quality"]["correct"],
                    "total": model["quality"]["total"],
                    "accuracy": model["quality"]["accuracy"],
                },
            )
    if json_output:
        console.print_json(data=report)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_cli_benchmark.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/omm/cli.py tests/test_cli_benchmark.py
git commit -m "feat: omm benchmark offers to upload results after a local run"
```

---

### Task 8: Server schema accepts optional quality fields

**Files:**
- Modify: `src/localfit_server/app.py:17-62` (`BenchmarkEvent`)
- Test: `tests/test_self_hosted_server.py`

**Interfaces:**
- Produces: `BenchmarkEvent` accepts 5 new optional fields (`quality_pack_id: str | None`, `quality_pack_version: str | None`, `quality_correct: int | None`, `quality_total: int | None`, `quality_accuracy: float | None`), validated all-or-nothing plus `quality_correct <= quality_total`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_self_hosted_server.py`:

```python
def _quality_fields():
    return {
        "quality_pack_id": "localfit-gsm8k-bilingual-smoke",
        "quality_pack_version": "1.1.0",
        "quality_correct": 6,
        "quality_total": 8,
        "quality_accuracy": 0.75,
    }


def test_self_hosted_collector_accepts_optional_quality_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALFIT_DB_PATH", str(tmp_path / "benchmarks.sqlite3"))
    server_app.get_store.cache_clear()
    client = TestClient(server_app.app)
    event = _event()
    event.update(_quality_fields())

    response = client.post("/v1/benchmarks", json=event)

    assert response.status_code == 201
    server_app.get_store.cache_clear()


def test_self_hosted_collector_rejects_partial_quality_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALFIT_DB_PATH", str(tmp_path / "benchmarks.sqlite3"))
    server_app.get_store.cache_clear()
    client = TestClient(server_app.app)
    event = _event()
    event["quality_correct"] = 6

    assert client.post("/v1/benchmarks", json=event).status_code == 422
    server_app.get_store.cache_clear()


def test_self_hosted_collector_rejects_correct_over_total(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALFIT_DB_PATH", str(tmp_path / "benchmarks.sqlite3"))
    server_app.get_store.cache_clear()
    client = TestClient(server_app.app)
    event = _event()
    event.update(_quality_fields())
    event["quality_correct"] = 9

    assert client.post("/v1/benchmarks", json=event).status_code == 422
    server_app.get_store.cache_clear()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_self_hosted_server.py -v`
Expected: FAIL — first new test gets 422 (`extra="forbid"` rejects the unknown `quality_*` keys); the other two get 201 instead of the expected 422.

- [ ] **Step 3: Implement**

In `src/localfit_server/app.py`, after `num_batch: int | None = Field(default=None, ge=1, le=1_000_000)` (line 38) add:

```python
    quality_pack_id: str | None = Field(default=None, max_length=100)
    quality_pack_version: str | None = Field(default=None, max_length=20)
    quality_correct: int | None = Field(default=None, ge=0, le=100)
    quality_total: int | None = Field(default=None, ge=1, le=100)
    quality_accuracy: float | None = Field(default=None, ge=0, le=1)
```

Extend `validate_sample_summary` (lines 51-62) with the quality checks — rename it slightly to keep it accurate, or add a second validator. Add a second `model_validator`:

```python
    @model_validator(mode="after")
    def validate_quality_summary(self) -> "BenchmarkEvent":
        quality_fields = (
            self.quality_pack_id,
            self.quality_pack_version,
            self.quality_correct,
            self.quality_total,
            self.quality_accuracy,
        )
        if any(f is not None for f in quality_fields) and any(f is None for f in quality_fields):
            raise ValueError("quality fields must all be supplied together")
        if self.quality_correct is not None and self.quality_total is not None:
            if self.quality_correct > self.quality_total:
                raise ValueError("quality_correct cannot exceed quality_total")
        return self
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_self_hosted_server.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/localfit_server/app.py tests/test_self_hosted_server.py
git commit -m "feat: accept optional quality-eval fields in the benchmark collector"
```

---

### Task 9: `omm contribute` benchmarks candidates with the quality evaluator

**Files:**
- Modify: `src/omm/cli.py:776-877` (`_install_impl`), `src/omm/cli.py:1786-1841` (`_run_contribution_loop`), `src/omm/cli.py:1899-1913` (`contribute()`, where the loop is kicked off)
- Test: `tests/test_install_impl.py`, `tests/test_contribute_loop.py`, `tests/test_cli_contribute.py`

**Interfaces:**
- Consumes: `quality_mod.evaluate_model(tag, pack, speed_runs=3) -> dict` (existing, `src/omm/quality.py:259`), `quality_mod.unload_model(tag) -> bool` (existing), `quality_mod.load_pack(path=None) -> tuple[dict, str]` (existing).
- Produces: `_install_impl(resolved, *, auto_upload=False, skip_unfit=False, stop_event=None, use_quality_eval=False, quality_pack=None)`. When `use_quality_eval=True` and the model links into Ollama, `tokens_per_sec` becomes the quality pack's median speed and the resulting `InstallOutcome` was reported to the server with a `quality` summary attached. `_run_contribution_loop(queue, stop_event, refetch, quality_pack=None)` — new optional 4th parameter, unused by existing tests (they stub `_install_impl` directly, so the extra kwarg it receives is irrelevant to them).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_install_impl.py`:

```python
def test_use_quality_eval_reports_median_speed_and_quality_summary(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    monkeypatch.setattr(cli, "download_file", lambda url, dest, stop_check=None: dest.write_bytes(b"x"))
    _stub_common(monkeypatch)
    fake_result = {
        "quality": {"correct": 6, "total": 8, "accuracy": 0.75},
        "speed": {
            "median_tokens_per_sec": 42.5,
            "samples_tokens_per_sec": [41.0, 42.5, 44.0],
            "runs": 3,
        },
    }
    monkeypatch.setattr(cli.quality_mod, "evaluate_model", lambda tag, pack, speed_runs=3: fake_result)
    unloaded = []
    monkeypatch.setattr(cli.quality_mod, "unload_model", lambda tag: unloaded.append(tag) or True)
    sent = []
    monkeypatch.setattr(cli.telemetry, "send_event", lambda event, force=False: sent.append(event) or True)

    outcome = cli._install_impl(
        _resolved(),
        auto_upload=True,
        use_quality_eval=True,
        quality_pack={"pack_id": "pack-1", "pack_version": "1.1.0", "items": []},
        stop_event=threading.Event(),
    )

    assert outcome.tokens_per_sec == 42.5
    assert unloaded == ["tinyllama"]
    event = sent[0]
    assert event["sample_count"] == 3
    assert event["tokens_per_sec_min"] == 41.0
    assert event["tokens_per_sec_max"] == 44.0
    assert event["quality_correct"] == 6
    assert event["quality_total"] == 8


def test_use_quality_eval_failure_reports_no_result(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    monkeypatch.setattr(cli, "download_file", lambda url, dest, stop_check=None: dest.write_bytes(b"x"))
    _stub_common(monkeypatch)

    def raise_eval(tag, pack, speed_runs=3):
        raise cli.quality_mod.QualityEvaluationError("ollama returned nothing")

    monkeypatch.setattr(cli.quality_mod, "evaluate_model", raise_eval)
    monkeypatch.setattr(cli.quality_mod, "unload_model", lambda tag: True)
    monkeypatch.setattr(
        cli.telemetry, "send_event", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no send"))
    )

    outcome = cli._install_impl(
        _resolved(),
        auto_upload=True,
        use_quality_eval=True,
        quality_pack={"pack_id": "pack-1", "pack_version": "1.1.0", "items": []},
        stop_event=threading.Event(),
    )

    assert outcome.tokens_per_sec is None
    assert outcome.telemetry_sent is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_install_impl.py -v -k quality_eval`
Expected: FAIL — `TypeError: _install_impl() got an unexpected keyword argument 'use_quality_eval'`.

- [ ] **Step 3: Implement `_install_impl`'s new branch**

In `src/omm/cli.py`, change the signature (`cli.py:776-782`):

```python
def _install_impl(
    resolved,
    *,
    auto_upload: bool = False,
    skip_unfit: bool = False,
    stop_event: threading.Event | None = None,
    use_quality_eval: bool = False,
    quality_pack: dict | None = None,
) -> InstallOutcome:
```

Replace the benchmarking block (`cli.py:847-876`, from `tokens_per_sec = None` through the final `else` branch) with:

```python
    tokens_per_sec = None
    telemetry_sent = False
    sample_count = 1
    speed_min = speed_max = None
    quality_summary = None
    if linked["ollama"]:
        console.print("Benchmarking...")
        if use_quality_eval:
            try:
                result = _run_interruptible(
                    lambda: quality_mod.evaluate_model(ollama_tag, quality_pack, speed_runs=3),
                    stop_event,
                )
            except _Interrupted as e:
                raise ContributionStopped(filename) from e
            except quality_mod.QualityEvaluationError:
                result = None
            finally:
                quality_mod.unload_model(ollama_tag)
            if result is not None:
                tokens_per_sec = result["speed"]["median_tokens_per_sec"]
                samples = result["speed"]["samples_tokens_per_sec"]
                sample_count = result["speed"]["runs"]
                speed_min, speed_max = min(samples), max(samples)
                quality_summary = {
                    "pack_id": quality_pack["pack_id"],
                    "pack_version": quality_pack.get("pack_version"),
                    "correct": result["quality"]["correct"],
                    "total": result["quality"]["total"],
                    "accuracy": result["quality"]["accuracy"],
                }
        else:
            try:
                tokens_per_sec = _run_interruptible(
                    lambda: benchmark.benchmark_ollama(ollama_tag), stop_event
                )
            except _Interrupted as e:
                raise ContributionStopped(filename) from e

        if tokens_per_sec:
            console.print(f"[cyan]{tokens_per_sec:.1f} tok/s[/cyan]")
            _maybe_auto_calibrate(filename, repo_id, dest, tokens_per_sec)

            want_upload = auto_upload or _resolve_upload_decision(
                "Send this machine's benchmark result to the server?"
            )
            if want_upload:
                telemetry_sent = _report_telemetry(
                    filename,
                    repo_id,
                    tokens_per_sec,
                    sample_count=sample_count,
                    speed_min=speed_min,
                    speed_max=speed_max,
                    quality=quality_summary,
                )
            else:
                telemetry.log_attempt("declined_by_user", filename)
        else:
            telemetry_sent = _report_telemetry(filename, repo_id, tokens_per_sec)
    else:
        telemetry.log_attempt("not_attempted_no_ollama_link", filename)
```

- [ ] **Step 4: Run tests to verify `_install_impl` tests pass**

Run: `pytest tests/test_install_impl.py -v`
Expected: all PASS (old tests unaffected since `use_quality_eval` defaults to `False`).

- [ ] **Step 5: Wire `omm contribute` to use it — write the failing test**

Add to `tests/test_cli_contribute.py`:

```python
def test_contribute_loads_quality_pack_and_passes_it_to_loop(isolated_omm_home, monkeypatch):
    config.update_config(telemetry_endpoint="https://example.com/telemetry.json")
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: True)
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(
        cli.predictor,
        "load_model_with_change_note",
        lambda url: ({"trees": [{}], "candidates": [{"repo_id": "o", "filename": "m.gguf"}]}, False),
    )
    monkeypatch.setattr(cli, "scan_hardware", lambda: object())
    monkeypatch.setattr(cli.predictor, "rank_candidates", lambda artifact, hw: [])
    monkeypatch.setattr(cli.benchmark_history, "loaded_refs", lambda: set())
    monkeypatch.setattr(cli, "_EscListener", _FakeListener)
    monkeypatch.setattr(cli, "_telemetry_row_count", lambda endpoint: 0)
    monkeypatch.setattr(cli, "autoremove", lambda: None)
    fake_pack = {"pack_id": "pack-1", "pack_version": "1.1.0", "items": []}
    monkeypatch.setattr(cli.quality_mod, "load_pack", lambda: (fake_pack, "sha"))

    captured = {}

    def fake_loop(queue, stop_event, refetch, quality_pack=None):
        captured["quality_pack"] = quality_pack
        return cli._ContributionStats(benchmarked=[])

    monkeypatch.setattr(cli, "_run_contribution_loop", fake_loop)

    result = runner.invoke(cli.app, ["contribute"])

    assert result.exit_code == 0, result.stdout
    assert captured["quality_pack"] == fake_pack
```

(`_FakeListener` is already defined at the top of this file.)

- [ ] **Step 6: Run test to verify it fails**

Run: `pytest tests/test_cli_contribute.py -v -k quality_pack`
Expected: FAIL — `_run_contribution_loop` isn't called with a `quality_pack` kwarg yet (or `load_pack` isn't called at all).

- [ ] **Step 7: Implement**

In `_run_contribution_loop` (`cli.py:1786`), add the parameter and pass it through to `_install_impl`:

```python
def _run_contribution_loop(
    queue, stop_event: threading.Event, refetch, quality_pack: dict | None = None
) -> _ContributionStats:
```

and change the `_install_impl` call (`cli.py:1802-1805`):

```python
        try:
            outcome = _install_impl(
                resolved,
                auto_upload=True,
                skip_unfit=True,
                stop_event=stop_event,
                use_quality_eval=True,
                quality_pack=quality_pack,
            )
```

In `contribute()` (`cli.py:1869` onward), right after the existing daemon-reachable check and before building the `ContributionQueue` (around `cli.py:1902`), load the pack once:

```python
    try:
        quality_pack, _ = quality_mod.load_pack()
    except quality_mod.QualityEvaluationError as error:
        console.print(f"[red]Could not load the quality pack: {error}[/red]")
        raise typer.Exit(1) from error
```

and pass it through the existing loop call (`cli.py:1913`):

```python
        stats = _run_contribution_loop(queue, listener.stop_event, refetch, quality_pack=quality_pack)
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `pytest tests/test_cli_contribute.py tests/test_contribute_loop.py tests/test_install_impl.py -v`
Expected: all PASS.

- [ ] **Step 9: Commit**

```bash
git add src/omm/cli.py tests/test_install_impl.py tests/test_cli_contribute.py
git commit -m "feat: omm contribute benchmarks with the quality+speed evaluator"
```

---

### Task 10: `omm contribute` policy gate (never blocks, always warns once)

**Files:**
- Modify: `src/omm/cli.py:1869-1889` (`contribute()`, before the existing disk/bandwidth warning)
- Test: `tests/test_cli_contribute.py`

**Interfaces:**
- Produces: `omm contribute` exits 1 immediately when `telemetry_send_policy == "never"`. When `"always"` and `contribute_always_ack` is not yet `True`, shows a one-time warning+confirm before the existing "Start contributing compute now?" prompt; declining exits 0; accepting persists `contribute_always_ack=True` and continues. Subsequent runs under `"always"` skip this extra prompt. Under `"ask"` (default), behavior is unchanged except the existing warning text now names the current policy.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_cli_contribute.py`:

```python
def test_contribute_refuses_when_policy_never(isolated_omm_home, monkeypatch):
    config.update_config(telemetry_send_policy="never")
    monkeypatch.setattr(
        cli, "_ask_confirm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no prompt"))
    )

    result = runner.invoke(cli.app, ["contribute"])

    assert result.exit_code == 1
    assert "requires benchmark uploads" in result.stdout


def test_contribute_warns_once_when_policy_always(isolated_omm_home, monkeypatch):
    config.update_config(telemetry_send_policy="always")
    confirms = []

    def fake_confirm(message, **k):
        confirms.append(message)
        return True

    monkeypatch.setattr(cli, "_ask_confirm", fake_confirm)
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(
        cli.predictor,
        "load_model_with_change_note",
        lambda url: ({"trees": [{}], "candidates": []}, False),
    )
    monkeypatch.setattr(cli, "scan_hardware", lambda: object())
    monkeypatch.setattr(cli.predictor, "rank_candidates", lambda artifact, hw: [])
    monkeypatch.setattr(cli.benchmark_history, "loaded_refs", lambda: set())
    monkeypatch.setattr(cli, "_EscListener", _FakeListener)
    monkeypatch.setattr(cli, "_telemetry_row_count", lambda endpoint: None)
    monkeypatch.setattr(cli, "autoremove", lambda: None)
    monkeypatch.setattr(cli.quality_mod, "load_pack", lambda: ({"pack_id": "p", "items": []}, "sha"))
    monkeypatch.setattr(cli, "_run_contribution_loop", lambda *a, **k: cli._ContributionStats(benchmarked=[]))

    result = runner.invoke(cli.app, ["contribute"])

    assert result.exit_code == 0, result.stdout
    assert any("always" in message.lower() for message in confirms)
    assert config.load_config()["contribute_always_ack"] is True


def test_contribute_skips_always_warning_once_acknowledged(isolated_omm_home, monkeypatch):
    config.update_config(telemetry_send_policy="always", contribute_always_ack=True)
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(
        cli.predictor,
        "load_model_with_change_note",
        lambda url: ({"trees": [{}], "candidates": []}, False),
    )
    monkeypatch.setattr(cli, "scan_hardware", lambda: object())
    monkeypatch.setattr(cli.predictor, "rank_candidates", lambda artifact, hw: [])
    monkeypatch.setattr(cli.benchmark_history, "loaded_refs", lambda: set())
    monkeypatch.setattr(cli, "_EscListener", _FakeListener)
    monkeypatch.setattr(cli, "_telemetry_row_count", lambda endpoint: None)
    monkeypatch.setattr(cli, "autoremove", lambda: None)
    monkeypatch.setattr(cli.quality_mod, "load_pack", lambda: ({"pack_id": "p", "items": []}, "sha"))
    monkeypatch.setattr(cli, "_run_contribution_loop", lambda *a, **k: cli._ContributionStats(benchmarked=[]))
    confirms = []
    monkeypatch.setattr(cli, "_ask_confirm", lambda message, **k: confirms.append(message) or True)

    result = runner.invoke(cli.app, ["contribute"])

    assert result.exit_code == 0, result.stdout
    assert not any("always" in message.lower() for message in confirms)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cli_contribute.py -v -k "policy_never or policy_always or acknowledged"`
Expected: FAIL — `omm contribute` doesn't check the policy yet, so the `never` case reaches the normal confirm instead of exiting 1, and the `always` case never shows an extra warning.

- [ ] **Step 3: Implement**

At the very top of `contribute()` (`src/omm/cli.py:1870`, before the existing `console.print("[yellow]This will repeatedly...")` block), insert:

```python
    policy = load_config().get("telemetry_send_policy", "ask")
    if policy == "never":
        console.print(
            "[red]omm contribute requires benchmark uploads to be enabled. "
            "Run `omm setting telemetry --enable` or `--ask` first.[/red]"
        )
        raise typer.Exit(1)
    if policy == "always" and not load_config().get("contribute_always_ack"):
        console.print(
            "[yellow]Upload policy is 'always' - every benchmark result from this "
            "and future omm contribute runs will be sent to the server without "
            "asking each time.[/yellow]"
        )
        if not _ask_confirm("Continue?"):
            console.print("[yellow]Cancelled.[/yellow]")
            raise typer.Exit(0)
        config_mod.update_config(contribute_always_ack=True)
```

Then update the existing warning text (`cli.py:1875-1879`) to name the policy:

```python
    console.print(
        "[yellow]This will repeatedly download, benchmark, and delete GGUF models "
        "until you press Esc. It uses real bandwidth, disk space, and compute, "
        "runs unattended (no per-model confirmation), and uploads every benchmark "
        f"result to the server per your current upload policy ({policy}).[/yellow]"
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_cli_contribute.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/omm/cli.py tests/test_cli_contribute.py
git commit -m "feat: gate omm contribute on the telemetry upload policy"
```

---

### Task 11: Full regression pass

**Files:** none (verification only)

- [ ] **Step 1: Run the entire suite**

Run: `pytest -q`
Expected: all tests PASS, no leftover references to `telemetry_opt_in`, `quality-eval`, or `quality_eval_cmd`.

- [ ] **Step 2: Grep for stragglers**

Run: `grep -rn "telemetry_opt_in\|quality-eval\|quality_eval_cmd" src/ tests/ README.md`
Expected: no matches (aside from historical docs under `docs/`, which are intentionally left alone per the spec).

- [ ] **Step 3: Commit if the grep step required any fixup**

```bash
git add -A
git commit -m "chore: clean up remaining quality-eval/telemetry_opt_in references"
```
(skip this step if nothing needed fixing)
