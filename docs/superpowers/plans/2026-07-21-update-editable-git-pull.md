# omm update: editable-clone git-pull Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `omm update` fast after the first run by switching from a
per-update `pipx install --force <git-URL>` (full clone+build+reinstall
every time) to a persistent `~/.omm/src` editable clone that later updates
via `git fetch`/`reset --hard` alone.

**Architecture:** `~/.omm/src` becomes a permanent, blobless git clone.
pipx installs it `--editable`, so its `.pth` file points straight at
`~/.omm/src/src` — any file change there is live immediately, no
reinstall. `omm update` migrates not-yet-converted installs once (clone +
`pipx install --editable`), then afterward just does `git fetch` + `git
reset --hard`, falling back to a full editable reinstall only if `pip
check` finds a genuinely new/changed dependency.

**Tech Stack:** Python 3.10+, Typer CLI, pipx, git, pytest (existing
`tests/test_cli_update.py` conventions: `typer.testing.CliRunner`,
`monkeypatch.setattr(cli, ...)`, `_FakeProc` for `subprocess.Popen`).

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-21-update-editable-git-pull-design.md`
  — every task below implements one numbered section of that spec; do not
  deviate from the code shown there without updating the spec first.
- No `_installed_commit()` caller outside `cli.py` needs to change — its
  signature (`() -> str | None`) is unchanged, only its internal logic.
- Every new subprocess call must have a `timeout` and must not let
  `subprocess.TimeoutExpired` propagate uncaught (existing convention in
  `_remote_head_commit`, `_deps_satisfied`).
- Run `.venv/bin/python -m pytest tests -q` after every task; it must stay
  green before moving to the next task.
- `~/.omm/src` does not exist on this dev machine yet (verified) — tests
  must not accidentally trigger a real `git clone` against it. Any test
  that reaches `update()`'s migration branch must explicitly monkeypatch
  `cli._src_head_commit` (to force fast-path) unless the migration branch
  is what's under test, in which case `cli._migrate_to_editable_install`
  itself must be monkeypatched or its internal `subprocess.run` calls
  mocked — never let a real `git clone`/`pipx install` run inside a test.

---

## Task 1: `SRC_DIR` constant + `_src_head_commit()`

**Files:**
- Modify: `src/omm/cli.py:49` (import), `src/omm/cli.py:201` (after `_BARE_REPO_URL`)
- Test: `tests/test_cli_update.py`

**Interfaces:**
- Produces: `cli.SRC_DIR: Path` (= `OMM_HOME / "src"`), `cli._src_head_commit() -> str | None`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_cli_update.py` (near the bottom, after the `_remote_head_commit` tests, before `test_run_pipx_install_advances_progress_on_known_stage_lines`):

```python
def test_src_head_commit_returns_none_when_git_dir_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "SRC_DIR", tmp_path / "src")

    assert cli._src_head_commit() is None


def test_src_head_commit_returns_head_when_git_dir_present(monkeypatch, tmp_path):
    src = tmp_path / "src"
    (src / ".git").mkdir(parents=True)
    monkeypatch.setattr(cli, "SRC_DIR", src)
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 0, stdout="deadbeef123\n", stderr=""),
    )

    assert cli._src_head_commit() == "deadbeef123"


def test_src_head_commit_returns_none_when_rev_parse_fails(monkeypatch, tmp_path):
    src = tmp_path / "src"
    (src / ".git").mkdir(parents=True)
    monkeypatch.setattr(cli, "SRC_DIR", src)
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 128, stdout="", stderr="fatal: not a git repository"),
    )

    assert cli._src_head_commit() is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_cli_update.py -k src_head_commit -v`
Expected: FAIL with `AttributeError: module 'omm.cli' has no attribute 'SRC_DIR'` (or `_src_head_commit`)

- [ ] **Step 3: Add the import, constant, and function**

In `src/omm/cli.py:49`, change:

```python
from omm.config import MODELS_DIR, load_config, save_config
```

to:

```python
from omm.config import MODELS_DIR, OMM_HOME, load_config, save_config
```

In `src/omm/cli.py`, right after the existing line `_BARE_REPO_URL = REPO_URL.removeprefix("git+")` (line 201), add:

```python
SRC_DIR = OMM_HOME / "src"


def _src_head_commit() -> str | None:
    """HEAD commit of the persistent editable clone at SRC_DIR, if this
    install has migrated to the git-pull update mechanism. None if not
    migrated yet, or if the clone is missing/corrupted (triggers
    self-healing re-migration in update())."""
    if not (SRC_DIR / ".git").exists():
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(SRC_DIR), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_cli_update.py -k src_head_commit -v`
Expected: 3 passed

- [ ] **Step 5: Run the full suite (must still be green — nothing else references SRC_DIR yet)**

Run: `.venv/bin/python -m pytest tests -q`
Expected: all pass (321 + 3 new = 324)

- [ ] **Step 6: Commit**

```bash
git add src/omm/cli.py tests/test_cli_update.py
git commit -m "feat: add SRC_DIR + _src_head_commit for the editable-clone update path"
```

---

## Task 2: `_installed_commit()` prefers the editable clone

**Files:**
- Modify: `src/omm/cli.py:204` (`_installed_commit`)
- Test: `tests/test_cli_update.py`

**Interfaces:**
- Consumes: `cli._src_head_commit() -> str | None` (Task 1)
- Produces: `cli._installed_commit()` behavior unchanged for callers (still `() -> str | None`), but now checks the clone first.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cli_update.py`, near the existing `test_installed_commit_*` tests:

```python
def test_installed_commit_prefers_src_head_over_direct_url_json(monkeypatch):
    monkeypatch.setattr(cli, "_src_head_commit", lambda: "from-src-clone")

    class _FakeDist:
        def read_text(self, name):
            return '{"url": "https://x", "vcs_info": {"commit_id": "from-direct-url"}}'

    monkeypatch.setattr(cli.importlib.metadata, "distribution", lambda name: _FakeDist())

    assert cli._installed_commit() == "from-src-clone"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_cli_update.py -k prefers_src_head -v`
Expected: FAIL — `_installed_commit()` returns `"from-direct-url"`, not `"from-src-clone"`

- [ ] **Step 3: Update `_installed_commit()`**

Replace `src/omm/cli.py:204-215`:

```python
def _installed_commit() -> str | None:
    """The commit `omm` was actually installed from, read from pip's PEP 610
    `direct_url.json` - present whenever pip installed from a VCS URL (i.e.
    every real `pipx install`). None for editable/local-path dev installs,
    which carry no vcs_info to compare against."""
    try:
        raw = importlib.metadata.distribution("omm").read_text("direct_url.json")
    except importlib.metadata.PackageNotFoundError:
        return None
    if not raw:
        return None
    return json.loads(raw).get("vcs_info", {}).get("commit_id")
```

with:

```python
def _installed_commit() -> str | None:
    """The commit omm is actually running from. Checks the persistent
    editable clone (SRC_DIR) first, then falls back to pip's PEP 610
    direct_url.json vcs_info - present for not-yet-migrated installs that
    still used a plain `pipx install <git-URL>` VCS snapshot."""
    src_commit = _src_head_commit()
    if src_commit:
        return src_commit
    try:
        raw = importlib.metadata.distribution("omm").read_text("direct_url.json")
    except importlib.metadata.PackageNotFoundError:
        return None
    if not raw:
        return None
    return json.loads(raw).get("vcs_info", {}).get("commit_id")
```

- [ ] **Step 4: Run the two other existing `_installed_commit` tests plus the new one**

Run: `.venv/bin/python -m pytest tests/test_cli_update.py -k installed_commit -v`
Expected: 4 passed (`test_installed_commit_reads_vcs_info_from_direct_url_json`,
`test_installed_commit_returns_none_for_editable_dev_install`,
`test_installed_commit_returns_none_when_package_not_found`,
`test_installed_commit_prefers_src_head_over_direct_url_json`) — the first
three still pass unmodified because on this dev machine
`(SRC_DIR / ".git").exists()` is `False` (verified: `~/.omm/src` does not
exist), so `_src_head_commit()` returns `None` and falls through to the
mocked `direct_url.json` path exactly as before.

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest tests -q`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add src/omm/cli.py tests/test_cli_update.py
git commit -m "feat: _installed_commit prefers the editable clone's HEAD"
```

---

## Task 3: `_install_spec()` targets the local clone

**Files:**
- Modify: `src/omm/cli.py:130-136` (`_install_spec`)
- Test: `tests/test_cli_update.py:21-30`

**Interfaces:**
- Consumes: `cli.SRC_DIR` (Task 1)
- Produces: `cli._install_spec() -> str` now returns a local path spec (`str(SRC_DIR)` or `f"{SRC_DIR}[nvidia]"`) instead of a git URL spec. Used by Task 4/5's pipx calls.

- [ ] **Step 1: Update the two existing tests**

Replace `tests/test_cli_update.py:21-30`:

```python
def test_install_spec_uses_bare_repo_url_on_darwin(monkeypatch):
    monkeypatch.setattr(cli.platform, "system", lambda: "Darwin")

    assert cli._install_spec() == cli.REPO_URL


def test_install_spec_adds_nvidia_extra_on_non_darwin(monkeypatch):
    monkeypatch.setattr(cli.platform, "system", lambda: "Linux")

    assert cli._install_spec() == f"omm[nvidia] @ {cli.REPO_URL}"
```

with:

```python
def test_install_spec_points_at_src_dir_on_darwin(monkeypatch):
    monkeypatch.setattr(cli.platform, "system", lambda: "Darwin")

    assert cli._install_spec() == str(cli.SRC_DIR)


def test_install_spec_adds_nvidia_extra_on_non_darwin(monkeypatch):
    monkeypatch.setattr(cli.platform, "system", lambda: "Linux")

    assert cli._install_spec() == f"{cli.SRC_DIR}[nvidia]"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_cli_update.py -k install_spec -v`
Expected: FAIL — `_install_spec()` still returns `cli.REPO_URL`-based strings

- [ ] **Step 3: Update `_install_spec()`**

Replace `src/omm/cli.py:130-136`:

```python
def _install_spec() -> str:
    """NVIDIA VRAM detection is dead weight on Mac (no NVIDIA GPUs since
    2016) - only pull that extra in on other platforms, mirroring
    install.sh."""
    if platform.system() == "Darwin":
        return REPO_URL
    return f"omm[nvidia] @ {REPO_URL}"
```

with:

```python
def _install_spec() -> str:
    """NVIDIA VRAM detection is dead weight on Mac (no NVIDIA GPUs since
    2016) - only pull that extra in on other platforms, mirroring
    install.sh. Points at the persistent local clone (SRC_DIR) rather than
    the git URL directly, since omm installs it --editable."""
    if platform.system() == "Darwin":
        return str(SRC_DIR)
    return f"{SRC_DIR}[nvidia]"
```

- [ ] **Step 4: Run tests, then the full suite**

Run: `.venv/bin/python -m pytest tests/test_cli_update.py -k install_spec -v`
Expected: 2 passed

Run: `.venv/bin/python -m pytest tests -q`
Expected: all pass (some `update()`-level tests that reference
`cli.REPO_URL` in their `calls == [...]` assertions will now fail, since
`_install_spec()` changed underneath them — that's expected here and gets
fixed in Task 5. If you're running this task in isolation, note the
failures and continue; do not fix them yet.)

- [ ] **Step 5: Commit**

```bash
git add src/omm/cli.py tests/test_cli_update.py
git commit -m "feat: _install_spec points at the local editable clone"
```

---

## Task 4: Migration + fast-path git helpers

**Files:**
- Modify: `src/omm/cli.py:3` (add `import shutil`), `src/omm/cli.py` (new functions after `_deps_satisfied`, before `_run_pipx_install_with_progress` — i.e. after current line 403)
- Test: `tests/test_cli_update.py`

**Interfaces:**
- Consumes: `cli.SRC_DIR`, `cli._BARE_REPO_URL`, `cli._install_spec()`, `cli._run_pipx_install_with_progress(args: list[str]) -> subprocess.CompletedProcess` (already exists)
- Produces: `cli._migrate_to_editable_install() -> subprocess.CompletedProcess`, `cli._git_update_src() -> subprocess.CompletedProcess`. Both used by Task 5's `update()`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_cli_update.py`:

```python
def test_migrate_to_editable_install_clones_then_pipx_installs(monkeypatch, tmp_path):
    src = tmp_path / "src"
    monkeypatch.setattr(cli, "SRC_DIR", src)
    monkeypatch.setattr(cli.platform, "system", lambda: "Darwin")
    run_calls = []
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda args, **kwargs: run_calls.append(args) or subprocess.CompletedProcess(args, 0, stdout="", stderr=""),
    )
    progress_calls = []
    monkeypatch.setattr(
        cli,
        "_run_pipx_install_with_progress",
        lambda args: progress_calls.append(args) or subprocess.CompletedProcess(args, 0, stdout="", stderr=""),
    )

    result = cli._migrate_to_editable_install()

    assert result.returncode == 0
    assert run_calls == [["git", "clone", "--filter=blob:none", "--quiet", cli._BARE_REPO_URL, str(src)]]
    assert progress_calls == [["pipx", "install", "--force", "--editable", str(src)]]


def test_migrate_to_editable_install_skips_pipx_when_clone_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "SRC_DIR", tmp_path / "src")
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 1, stdout="", stderr="clone failed"),
    )
    progress_calls = []
    monkeypatch.setattr(cli, "_run_pipx_install_with_progress", lambda args: progress_calls.append(args))

    result = cli._migrate_to_editable_install()

    assert result.returncode == 1
    assert progress_calls == []


def test_git_update_src_fetches_then_resets(monkeypatch, tmp_path):
    src = tmp_path / "src"
    monkeypatch.setattr(cli, "SRC_DIR", src)
    run_calls = []
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda args, **kwargs: run_calls.append(args) or subprocess.CompletedProcess(args, 0, stdout="", stderr=""),
    )

    result = cli._git_update_src()

    assert result.returncode == 0
    assert run_calls == [
        ["git", "-C", str(src), "fetch", "--quiet", "origin", "main"],
        ["git", "-C", str(src), "reset", "--hard", "--quiet", "origin/main"],
    ]


def test_git_update_src_stops_after_fetch_failure(monkeypatch, tmp_path):
    src = tmp_path / "src"
    monkeypatch.setattr(cli, "SRC_DIR", src)
    run_calls = []

    def fake_run(args, **kwargs):
        run_calls.append(args)
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="fetch failed")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    result = cli._git_update_src()

    assert result.returncode == 1
    assert len(run_calls) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_cli_update.py -k "migrate_to_editable or git_update_src" -v`
Expected: FAIL with `AttributeError: module 'omm.cli' has no attribute '_migrate_to_editable_install'` (or `_git_update_src`)

- [ ] **Step 3: Add `import shutil`**

In `src/omm/cli.py:3`, change:

```python
import importlib.metadata
import json
import platform
import subprocess
```

to:

```python
import importlib.metadata
import json
import platform
import shutil
import subprocess
```

- [ ] **Step 4: Add the two helper functions**

In `src/omm/cli.py`, right after the `_run_pipx_install_with_progress` function (currently ends around line 418, right before `@app.command()` / `def update()`), add:

```python
def _migrate_to_editable_install() -> subprocess.CompletedProcess:
    """First-run (or self-heal) path: (re)clone the repo into SRC_DIR and
    pipx --editable-install it, so future `omm update` calls are a `git
    pull` instead of a full pipx reinstall. Runs whenever SRC_DIR isn't a
    valid git checkout - regardless of whether the currently installed
    commit already matches latest, since the goal is switching mechanism,
    not code."""
    console.print("[cyan]Migrating to fast-update mode (one-time)...[/cyan]")
    shutil.rmtree(SRC_DIR, ignore_errors=True)
    try:
        clone = subprocess.run(
            ["git", "clone", "--filter=blob:none", "--quiet", _BARE_REPO_URL, str(SRC_DIR)],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess([], 1, stdout="", stderr="git clone timed out")
    if clone.returncode != 0:
        return clone
    return _run_pipx_install_with_progress(
        ["pipx", "install", "--force", "--editable", _install_spec()]
    )


def _git_update_src() -> subprocess.CompletedProcess:
    """Fast path for an already-migrated install: fetch + fast-forward the
    persistent clone in place. The editable install's .pth points straight
    at SRC_DIR/src, so this alone is enough to pick up code changes - no
    pipx call needed unless dependencies themselves changed (checked by
    the caller via _deps_satisfied())."""
    for args in (
        ["git", "-C", str(SRC_DIR), "fetch", "--quiet", "origin", "main"],
        ["git", "-C", str(SRC_DIR), "reset", "--hard", "--quiet", "origin/main"],
    ):
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="git command timed out")
        if result.returncode != 0:
            return result
    return result
```

- [ ] **Step 5: Run the new tests**

Run: `.venv/bin/python -m pytest tests/test_cli_update.py -k "migrate_to_editable or git_update_src" -v`
Expected: 4 passed

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/python -m pytest tests -q`
Expected: same pass/fail state as end of Task 3 (these are new, additive tests; the pre-existing `update()`-level failures from Task 3 are still expected and untouched until Task 5)

- [ ] **Step 7: Commit**

```bash
git add src/omm/cli.py tests/test_cli_update.py
git commit -m "feat: add migration and fast-path git helpers for omm update"
```

---

## Task 5: Rewrite `update()` and fix its tests

**Files:**
- Modify: `src/omm/cli.py:422-459` (the `update` command — line numbers shifted by earlier tasks, locate via `@app.command()` immediately above `def update()`)
- Test: `tests/test_cli_update.py`

**Interfaces:**
- Consumes: `cli._src_head_commit`, `cli._installed_commit`, `cli._remote_head_commit`, `cli.version_check.record`, `cli._migrate_to_editable_install`, `cli._git_update_src`, `cli._deps_satisfied`, `cli._run_pipx_install_with_progress`, `cli._install_spec`, `cli._refresh_data` (all already exist per Tasks 1-4)
- Produces: `update()` typer command, unchanged CLI signature (`omm update`, no args)

- [ ] **Step 1: Replace `update()`**

Find the current `update()` command in `src/omm/cli.py` (starts with
`@app.command()` / `def update() -> None:`, currently around line 422) and
replace its full body with:

```python
@app.command()
def update() -> None:
    """Reinstall omm from the latest source, then refresh rules/model data.
    Uses a persistent editable clone (SRC_DIR) for a git-pull-speed update
    once migrated; a one-time pipx --editable install otherwise."""
    migrated = _src_head_commit() is not None
    installed = _installed_commit()
    latest = _remote_head_commit() if installed else None
    if latest:
        version_check.record(latest)
    if migrated and installed and latest and installed == latest:
        console.print(f"[green]omm is already up to date ({installed[:7]}).[/green]")
        _refresh_data()
        return

    try:
        if not migrated:
            result = _migrate_to_editable_install()
        else:
            console.print(f"Updating omm from {REPO_URL} ...")
            result = _git_update_src()
            if result.returncode == 0 and not _deps_satisfied():
                result = _run_pipx_install_with_progress(
                    ["pipx", "install", "--force", "--editable", _install_spec()]
                )
    except FileNotFoundError:
        console.print(
            "[red]git or pipx not found. Install them first, or rerun the installer:[/red]\n"
            "  curl -fsSL https://raw.githubusercontent.com/minigu5/Localfit/main/install.sh | sh"
        )
        raise typer.Exit(1)

    if result.returncode != 0:
        console.print(f"[red]Update failed:[/red]\n{result.stderr}")
        raise typer.Exit(1)

    console.print("[green]omm reinstalled from the latest source.[/green]")
    _refresh_data()
```

- [ ] **Step 2: Delete the two now-redundant tests that directly asserted the old single pipx-install call shape**

In `tests/test_cli_update.py`, delete these two whole test functions (they
tested exact `pipx install --force <URL>` args — that call shape no longer
exists as a single path; their behavior is re-covered by the migration/
fast-path tests below):
- `test_update_reinstalls_via_pipx_then_refreshes_data`
- `test_update_reinstalls_when_installed_commit_differs_from_remote`
- `test_update_falls_back_to_full_install_when_deps_missing_after_no_deps_install`

- [ ] **Step 3: Update the two "already up to date" / "stale cache" tests to force fast-path**

Find `test_update_skips_reinstall_when_already_up_to_date` and add one
line so it explicitly simulates a migrated install (otherwise `update()`
now takes the migration branch and the `popen_calls == []` assertion
breaks, since migration doesn't call `subprocess.Popen` either but does
call `subprocess.run` for `git clone`, which isn't mocked and would try a
real clone):

```python
def test_update_skips_reinstall_when_already_up_to_date(monkeypatch):
    same_commit = "abc1234" * 5 + "abc12345"
    monkeypatch.setattr(cli, "_src_head_commit", lambda: same_commit)
    monkeypatch.setattr(cli, "_installed_commit", lambda: same_commit)
    monkeypatch.setattr(cli, "_remote_head_commit", lambda: same_commit)
    popen_calls = []
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **k: popen_calls.append(a) or _FakeProc([]))
    refresh_calls = []
    monkeypatch.setattr(cli, "_refresh_data", lambda: refresh_calls.append(1))

    result = runner.invoke(cli.app, ["update"])

    assert result.exit_code == 0, result.stdout
    assert "up to date" in result.stdout.lower()
    assert popen_calls == []
    assert refresh_calls == [1]
```

(Change from the original: added the `_src_head_commit` monkeypatch line,
and changed `monkeypatch.setattr(cli, "_installed_commit", lambda: "abc1234" * 5 + "abc12345")`
to use the shared `same_commit` variable so it matches `_src_head_commit`'s
return value — they represent the same underlying state now.)

Similarly update `test_update_refreshes_stale_cache_with_live_remote_head`:

```python
def test_update_refreshes_stale_cache_with_live_remote_head(monkeypatch):
    """A background check that ran before this `update` populated
    update_check.json with a now-outdated remote head. update() fetches
    the remote head live - it must write that fresh value back into the
    cache, or the next command's background check keeps serving the
    stale pre-update reading (false "Update available") until the TTL
    expires."""
    same_commit = "abc1234" * 5 + "abc12345"
    monkeypatch.setattr(cli, "_src_head_commit", lambda: same_commit)
    monkeypatch.setattr(cli, "_installed_commit", lambda: same_commit)
    monkeypatch.setattr(cli, "_remote_head_commit", lambda: same_commit)
    monkeypatch.setattr(cli, "_refresh_data", lambda: None)
    recorded = []
    monkeypatch.setattr(cli.version_check, "record", lambda head: recorded.append(head))

    result = runner.invoke(cli.app, ["update"])

    assert result.exit_code == 0, result.stdout
    assert recorded == [same_commit]
```

(Only change: added the `_src_head_commit` monkeypatch line.)

- [ ] **Step 4: Fix the pipx-missing / pipx-failure tests to go through the fast path**

Replace `test_update_reports_error_when_pipx_missing`:

```python
def test_update_reports_error_when_pipx_missing(monkeypatch):
    monkeypatch.setattr(cli, "_src_head_commit", lambda: "abc1234" * 5 + "abc12345")
    monkeypatch.setattr(cli, "_installed_commit", lambda: "old" * 13 + "old")
    monkeypatch.setattr(cli, "_remote_head_commit", lambda: "new" * 13 + "new")
    monkeypatch.setattr(
        cli,
        "_git_update_src",
        lambda: subprocess.CompletedProcess([], 0, stdout="", stderr=""),
    )
    monkeypatch.setattr(cli, "_deps_satisfied", lambda: False)

    def _raise(*args, **kwargs):
        raise FileNotFoundError("pipx")

    monkeypatch.setattr(cli.subprocess, "Popen", _raise)
    refresh_calls = []
    monkeypatch.setattr(cli, "_refresh_data", lambda: refresh_calls.append(1))

    result = runner.invoke(cli.app, ["update"])

    assert result.exit_code == 1
    assert "not found" in result.stdout
    assert refresh_calls == []
```

Replace `test_update_reports_error_and_skips_data_refresh_on_pipx_failure`:

```python
def test_update_reports_error_and_skips_data_refresh_on_pipx_failure(monkeypatch):
    monkeypatch.setattr(cli, "_src_head_commit", lambda: "abc1234" * 5 + "abc12345")
    monkeypatch.setattr(cli, "_installed_commit", lambda: "old" * 13 + "old")
    monkeypatch.setattr(cli, "_remote_head_commit", lambda: "new" * 13 + "new")
    monkeypatch.setattr(
        cli,
        "_git_update_src",
        lambda: subprocess.CompletedProcess([], 0, stdout="", stderr=""),
    )
    monkeypatch.setattr(cli, "_deps_satisfied", lambda: False)
    monkeypatch.setattr(
        cli.subprocess,
        "Popen",
        lambda args, **kwargs: _FakeProc(["boom\n"], returncode=1),
    )
    refresh_calls = []
    monkeypatch.setattr(cli, "_refresh_data", lambda: refresh_calls.append(1))

    result = runner.invoke(cli.app, ["update"])

    assert result.exit_code == 1
    assert "boom" in result.stdout
    assert refresh_calls == []
```

- [ ] **Step 5: Add the new orchestration tests**

Add to `tests/test_cli_update.py`:

```python
def test_update_migrates_when_not_yet_migrated_even_if_commit_matches(monkeypatch):
    """Migration must run purely because SRC_DIR isn't set up yet - even
    when the old-style installed commit already equals latest, since the
    point of migrating is switching update *mechanism*, not code."""
    same_commit = "abc1234" * 5 + "abc12345"
    monkeypatch.setattr(cli, "_src_head_commit", lambda: None)
    monkeypatch.setattr(cli, "_installed_commit", lambda: same_commit)
    monkeypatch.setattr(cli, "_remote_head_commit", lambda: same_commit)
    migrate_calls = []
    monkeypatch.setattr(
        cli,
        "_migrate_to_editable_install",
        lambda: migrate_calls.append(1) or subprocess.CompletedProcess([], 0, stdout="", stderr=""),
    )
    refresh_calls = []
    monkeypatch.setattr(cli, "_refresh_data", lambda: refresh_calls.append(1))

    result = runner.invoke(cli.app, ["update"])

    assert result.exit_code == 0, result.stdout
    assert migrate_calls == [1]
    assert refresh_calls == [1]
    assert "reinstalled" in result.stdout.lower()


def test_update_fast_path_skips_pipx_when_deps_unaffected(monkeypatch):
    monkeypatch.setattr(cli, "_src_head_commit", lambda: "abc1234" * 5 + "abc12345")
    monkeypatch.setattr(cli, "_installed_commit", lambda: "old" * 13 + "old")
    monkeypatch.setattr(cli, "_remote_head_commit", lambda: "new" * 13 + "new")
    git_calls = []
    monkeypatch.setattr(
        cli,
        "_git_update_src",
        lambda: git_calls.append(1) or subprocess.CompletedProcess([], 0, stdout="", stderr=""),
    )
    monkeypatch.setattr(cli, "_deps_satisfied", lambda: True)
    pipx_calls = []
    monkeypatch.setattr(cli, "_run_pipx_install_with_progress", lambda args: pipx_calls.append(args))
    refresh_calls = []
    monkeypatch.setattr(cli, "_refresh_data", lambda: refresh_calls.append(1))

    result = runner.invoke(cli.app, ["update"])

    assert result.exit_code == 0, result.stdout
    assert git_calls == [1]
    assert pipx_calls == []
    assert refresh_calls == [1]


def test_update_fast_path_falls_back_to_pipx_when_deps_changed(monkeypatch):
    monkeypatch.setattr(cli, "_src_head_commit", lambda: "abc1234" * 5 + "abc12345")
    monkeypatch.setattr(cli, "_installed_commit", lambda: "old" * 13 + "old")
    monkeypatch.setattr(cli, "_remote_head_commit", lambda: "new" * 13 + "new")
    monkeypatch.setattr(
        cli,
        "_git_update_src",
        lambda: subprocess.CompletedProcess([], 0, stdout="", stderr=""),
    )
    monkeypatch.setattr(cli, "_deps_satisfied", lambda: False)
    pipx_calls = []
    monkeypatch.setattr(
        cli,
        "_run_pipx_install_with_progress",
        lambda args: pipx_calls.append(args) or subprocess.CompletedProcess(args, 0, stdout="", stderr=""),
    )
    refresh_calls = []
    monkeypatch.setattr(cli, "_refresh_data", lambda: refresh_calls.append(1))

    result = runner.invoke(cli.app, ["update"])

    assert result.exit_code == 0, result.stdout
    assert pipx_calls == [["pipx", "install", "--force", "--editable", cli._install_spec()]]
    assert refresh_calls == [1]


def test_update_reports_error_when_git_update_fails(monkeypatch):
    monkeypatch.setattr(cli, "_src_head_commit", lambda: "abc1234" * 5 + "abc12345")
    monkeypatch.setattr(cli, "_installed_commit", lambda: "old" * 13 + "old")
    monkeypatch.setattr(cli, "_remote_head_commit", lambda: "new" * 13 + "new")
    monkeypatch.setattr(
        cli,
        "_git_update_src",
        lambda: subprocess.CompletedProcess([], 1, stdout="", stderr="fetch failed"),
    )
    refresh_calls = []
    monkeypatch.setattr(cli, "_refresh_data", lambda: refresh_calls.append(1))

    result = runner.invoke(cli.app, ["update"])

    assert result.exit_code == 1
    assert "fetch failed" in result.stdout
    assert refresh_calls == []
```

- [ ] **Step 6: Run the update tests**

Run: `.venv/bin/python -m pytest tests/test_cli_update.py -v`
Expected: all pass (count = previous total minus 3 deleted plus 4 new)

- [ ] **Step 7: Run the full suite**

Run: `.venv/bin/python -m pytest tests -q`
Expected: all pass

- [ ] **Step 8: Commit**

```bash
git add src/omm/cli.py tests/test_cli_update.py
git commit -m "feat: omm update uses git pull on the fast path, migrates once"
```

---

## Task 6: `install.sh` installs editable from the start

**Files:**
- Modify: `install.sh`

**Interfaces:**
- None (shell script, no Python interface)

- [ ] **Step 1: Replace the `REPO_URL`/`INSTALL_SPEC` header block**

Replace `install.sh` lines 5-13:

```sh
REPO_URL="git+https://github.com/minigu5/Localfit.git"

# NVIDIA VRAM detection is dead weight on Mac (no NVIDIA GPUs since 2016) -
# only pull that extra in on other platforms.
if [ "$(uname -s)" = "Darwin" ]; then
    INSTALL_SPEC="$REPO_URL"
else
    INSTALL_SPEC="omm[nvidia] @ $REPO_URL"
fi
```

with:

```sh
REPO_URL="https://github.com/minigu5/Localfit.git"
SRC_DIR="$HOME/.omm/src"
```

- [ ] **Step 2: Replace the install invocation near the end of the file**

Replace:

```sh
echo "Installing omm from $REPO_URL ..."
run_pipx install --force "$INSTALL_SPEC"
```

with:

```sh
echo "Cloning omm source to $SRC_DIR ..."
rm -rf "$SRC_DIR"
git clone --filter=blob:none --quiet "$REPO_URL" "$SRC_DIR"

# NVIDIA VRAM detection is dead weight on Mac (no NVIDIA GPUs since 2016) -
# only pull that extra in on other platforms.
if [ "$(uname -s)" = "Darwin" ]; then
    INSTALL_SPEC="$SRC_DIR"
else
    INSTALL_SPEC="$SRC_DIR[nvidia]"
fi

echo "Installing omm (editable) from $SRC_DIR ..."
run_pipx install --force --editable "$INSTALL_SPEC"
```

- [ ] **Step 3: Shell-syntax check**

Run: `sh -n install.sh`
Expected: no output (syntax OK)

If `shellcheck` is installed, also run: `shellcheck install.sh` and fix any
new warnings introduced by this change (pre-existing warnings unrelated to
this diff are out of scope).

- [ ] **Step 4: Commit**

```bash
git add install.sh
git commit -m "feat: install.sh clones omm and installs it --editable"
```

---

## Task 7: Manual end-to-end verification

**Files:** none (verification only, no code changes)

**Interfaces:** none

- [ ] **Step 1: Confirm current real install state, then simulate a fresh (not-yet-migrated) install**

```bash
ls ~/.omm/src 2>&1   # expect: No such file or directory
which omm             # note the path, e.g. /Users/you/.local/bin/omm
```

- [ ] **Step 2: Run the real migration path**

```bash
omm update
```

Expected: prints `Migrating to fast-update mode (one-time)...`, then the
pipx progress bar, then `omm reinstalled from the latest source.`, then
the rules/model refresh lines. `time omm update` on a *second* immediate
run should now print `omm is already up to date (...)` in well under 2
seconds (fast path, no pipx call).

```bash
ls ~/.omm/src/.git    # expect: this now exists
git -C ~/.omm/src log -1 --oneline
```

- [ ] **Step 3: Verify editable wiring**

```bash
find "$HOME/Library/Application Support/pipx/venvs/omm" -iname "_editable_impl_omm.pth" -exec cat {} \;
```

Expected: prints `~/.omm/src/src` (the persistent clone's `src/`
directory) — confirms `omm`'s installed package really is a live pointer
at the clone, not a copied snapshot.

- [ ] **Step 4: Verify the fast-forward-with-real-change path**

```bash
git -C ~/.omm/src reset --hard HEAD~1   # move the local clone one commit behind
omm update
```

Expected: prints `Updating omm from ... ...`, then a fast `git
fetch`/`reset --hard` (no pipx progress bar this time, since deps didn't
change), then `omm reinstalled from the latest source.`. Confirm timing
is on the order of ~1-2 seconds, not ~6-9.

```bash
git -C ~/.omm/src log -1 --oneline   # back at the latest commit
```

- [ ] **Step 5: Verify self-healing on a corrupted clone**

```bash
rm -rf ~/.omm/src/.git
omm update
```

Expected: prints `Migrating to fast-update mode (one-time)...` again
(self-heals), succeeds.

- [ ] **Step 6: Sanity-check the CLI still works end to end**

```bash
omm scan
omm --help
```

Expected: both run normally, no import errors, no crash — confirms the
editable install's entry points and module resolution are intact.

- [ ] **Step 7: Report results**

No commit for this task (verification only). Summarize the observed
timings and confirm all steps above matched expectations before
considering the feature done.
