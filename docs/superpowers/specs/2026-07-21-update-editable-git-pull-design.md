# `omm update`를 영구 clone + editable 설치 기반 git-pull로 전환

## 배경

[[project-omm]]. 방금 전 커밋(`6faacad`)에서 `omm update`의 `pipx install --force`
호출에 `--pip-args=--no-deps`를 추가해 9.3초 → 5.9초로 줄였다. 남은 5.9초의
정체는 pipx 구조 자체다: `pipx install --force <git-URL>`은 매번 (1) 패키지
이름 확인용 임시 venv에서 한 번, (2) 실제 설치용 venv에서 또 한 번 - git
clone과 wheel build를 **두 번** 하고 그때마다 venv도 새로 만든다. 이 리포는
빌드 스텝이 없는 순수 Python 패키지라 이 비용은 전부 낭비다.

직접 실측한 대안: pipx에 리포를 **영구 로컬 clone**으로 `--editable` 설치해
두면, site-packages에는 `src/`를 직접 가리키는 `.pth` 파일 하나만 생긴다
(`~/.omm/src` 안에서 `git pull`만 해도 재설치 없이 즉시 반영됨을 확인). 실측:

| 방식 | 시간 |
|---|---|
| 지금 (`pipx install --force <URL>`, `--no-deps` 포함) | 5.9초 |
| editable 최초 전환 (clone + venv + deps, 1회성) | 5.9초 |
| 이후 업데이트, 변경 없음 (`git pull`) | 0.5초 |
| 이후 업데이트, 새 커밋 있음 (blobless clone 기준) | ~1.3초 |

## 아키텍처

```
~/.omm/src/              # 영구 git clone (--filter=blob:none), 소스 오브 트루스
~/.omm/config.json       # 기존 그대로
pipx venv 'omm'          # --editable ~/.omm/src 로 설치, .pth가 src/ 를 직접 가리킴
```

`omm update`가 두 경로로 분기한다:

- **마이그레이션 경로** (`~/.omm/src`가 없거나 손상됨): clone 후
  `pipx install --force --editable`. 1회성, 지금과 같은 속도(~6~9초).
- **fast path** (이미 전환됨): `git fetch && git reset --hard`만 실행.
  `pip check`로 의존성 확인 후, 이번 커밋이 실제로 의존성을 바꿨을 때만
  editable 재설치로 폴백.

새로 설치하는 사람은 `install.sh`가 처음부터 이 방식으로 설치하므로
마이그레이션 자체를 겪지 않는다.

## 1. `src/omm/cli.py` - 상수 및 경로

- `from omm.config import MODELS_DIR, load_config, save_config` →
  `from omm.config import MODELS_DIR, OMM_HOME, load_config, save_config`
  (`OMM_HOME = Path.home() / ".omm"`, `config.py:11`에 이미 존재).
- `import shutil` 추가 (마이그레이션 시 손상된 `SRC_DIR` 정리용).
- `_BARE_REPO_URL = REPO_URL.removeprefix("git+")` (`cli.py:201`) 바로 아래에:

```python
SRC_DIR = OMM_HOME / "src"
```

`REPO_URL`/`_BARE_REPO_URL`은 그대로 유지 (여전히 `_remote_head_commit`의
`git ls-remote`와 마이그레이션 시 `git clone`에 쓰임).

## 2. `_installed_commit()` - editable 설치 우선 판별

새 헬퍼를 `_installed_commit()` (`cli.py:204`) 바로 위에 추가:

```python
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

`_installed_commit()` 본문 수정 - editable clone이 있으면 그걸 우선:

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

`_maybe_start_update_check`(`cli.py:256`), `_maybe_auto_import`(`cli.py:282`)
등 다른 호출부는 `_installed_commit()` 시그니처가 그대로라 변경 불필요.

## 3. `_install_spec()` - editable 대상 경로로 변경

```python
def _install_spec() -> str:
    """NVIDIA VRAM detection is dead weight on Mac (no NVIDIA GPUs since
    2016) - only pull that extra in on other platforms, mirroring
    install.sh. Points at the persistent local clone (SRC_DIR), since omm
    installs it --editable rather than from the git URL directly."""
    if platform.system() == "Darwin":
        return str(SRC_DIR)
    return f"{SRC_DIR}[nvidia]"
```

## 4. 마이그레이션 + fast path 헬퍼

`update()` 위, `_deps_satisfied()`(`cli.py:391`) 아래에 추가:

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

## 5. `update()` 재작성

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

`migrated`을 `.git` 존재 여부가 아니라 `_src_head_commit() is not None`(실제
`rev-parse HEAD` 성공 여부)으로 판단 - clone이 중간에 끊기거나 손상된 경우도
"마이그레이션 안 됨"으로 취급해 `_migrate_to_editable_install()`이 `shutil.rmtree`
후 재clone하며 자가 복구한다.

## 6. `install.sh` - 처음부터 editable로 설치

```sh
REPO_URL="https://github.com/minigu5/Localfit.git"   # git+ 접두사 제거 (git clone은 순수 URL 필요)
SRC_DIR="$HOME/.omm/src"
```

`echo "Installing omm from $REPO_URL ..."` / `run_pipx install --force
"$INSTALL_SPEC"` (파일 끝부분) 대체:

```sh
echo "Cloning omm source to $SRC_DIR ..."
rm -rf "$SRC_DIR"
git clone --filter=blob:none --quiet "$REPO_URL" "$SRC_DIR"

if [ "$(uname -s)" = "Darwin" ]; then
    INSTALL_SPEC="$SRC_DIR"
else
    INSTALL_SPEC="$SRC_DIR[nvidia]"
fi

echo "Installing omm (editable) from $SRC_DIR ..."
run_pipx install --force --editable "$INSTALL_SPEC"
```

파일 앞부분의 `INSTALL_SPEC` 사전 계산 블록(Darwin/nvidia 분기)은 삭제 -
위치만 clone 이후로 옮긴 것.

## 7. 영향받지 않는 것

- `_remote_head_commit()`, `_cached_remote_head_commit()`, `version_check`
  캐싱, `_maybe_start_update_check()`, `_bg_version_check_cmd` - 그대로.
  이들은 여전히 `git ls-remote`(clone 없이)로 "최신 커밋이 뭔지"만 확인하는
  경량 체크라 마이그레이션 여부와 무관하게 동작.
- `_refresh_data()`, `_deps_satisfied()`, `_run_pipx_install`,
  `_run_pipx_install_with_progress`, `_PIPX_INSTALL_STAGES` - 그대로 재사용.
  pipx가 `--editable` 설치 시에도 같은 stage 문구(`creating virtual
  environment...` 등)를 출력함을 직접 확인함.
- `pipx uninstall omm`은 `~/.omm/src`를 지우지 않음 - `~/.omm/config.json`도
  이미 그런 것처럼 기존 동작과 동일, 새로운 문제 아님.

## 8. 테스트 변경 (`tests/test_cli_update.py`)

- `test_install_spec_uses_bare_repo_url_on_darwin` /
  `test_install_spec_adds_nvidia_extra_on_non_darwin`: 기대값을
  `str(cli.SRC_DIR)` / `f"{cli.SRC_DIR}[nvidia]"`로 교체.
- 새 단위 테스트 (각각 `subprocess.run`을 직접 monkeypatch, git/pipx 호출을
  라우팅):
  - `test_src_head_commit_returns_head_when_git_dir_present`
  - `test_src_head_commit_returns_none_when_git_dir_missing`
  - `test_src_head_commit_returns_none_when_rev_parse_fails`
  - `test_installed_commit_prefers_src_head_over_direct_url_json`
  - `test_migrate_to_editable_install_clones_then_pipx_installs`
  - `test_migrate_to_editable_install_skips_pipx_when_clone_fails`
  - `test_git_update_src_fetches_then_resets`
  - `test_git_update_src_stops_after_fetch_failure`
- `update()` 오케스트레이션 테스트 - 기존 스타일대로 `cli._src_head_commit`,
  `cli._migrate_to_editable_install`, `cli._git_update_src`,
  `cli._deps_satisfied`를 직접 monkeypatch (raw subprocess 대신, git 명령어
  구성 자체는 위 단위 테스트가 커버):
  - `test_update_migrates_when_not_yet_migrated_even_if_commit_matches`
    (`_src_head_commit`이 None → `installed == latest`여도 마이그레이션 실행)
  - `test_update_fast_path_skips_pipx_when_deps_unaffected`
  - `test_update_fast_path_falls_back_to_pipx_when_deps_changed`
  - `test_update_reports_error_when_git_update_fails`
  - 기존 `test_update_skips_reinstall_when_already_up_to_date`,
    `test_update_refreshes_stale_cache_with_live_remote_head`: `_src_head_commit`을
    `lambda: "abc..."`(migrated)로 monkeypatch 추가해야 함 (안 그러면 이제
    마이그레이션 경로를 타서 어서션이 깨짐).
  - 기존 `test_update_reinstalls_via_pipx_then_refreshes_data`,
    `test_update_reinstalls_when_installed_commit_differs_from_remote`,
    `test_update_falls_back_to_full_install_when_deps_missing_after_no_deps_install`:
    이제 마이그레이션 경로 자체를 테스트하는 케이스로 재해석되거나
    (`_src_head_commit` mock 없음 → not migrated), fast path 케이스로
    다시 작성 - 위 새 오케스트레이션 테스트들과 통합/정리.
  - **중요**: `_src_head_commit`을 명시적으로 monkeypatch하지 않으면
    `update()`가 실제로 `not migrated` 경로를 타서 `_migrate_to_editable_install()`이
    진짜 `git clone`(네트워크 호출)을 시도한다. `test_update_reports_error_when_pipx_missing`
    (Popen이 FileNotFoundError를 던지는 케이스)과
    `test_update_reports_error_and_skips_data_refresh_on_pipx_failure`
    (Popen이 returncode=1인 케이스)는 원래 "pipx 자체가 없거나 실패하는"
    시나리오를 테스트하려는 의도이므로, `cli._src_head_commit`을 migrated
    커밋 문자열로 monkeypatch해 fast path로 보낸 뒤 `cli._deps_satisfied`를
    `False`로 monkeypatch해서 `_run_pipx_install_with_progress` 호출까지
    도달시켜야 한다 (그래야 실제 git 호출 없이 원래 의도한 pipx 실패
    시나리오만 검증됨).

## 9. 수동 검증

- `.venv/bin/pip install -e .`로 개발 체크아웃에 영향 주지 않고, 실제
  `pipx`로 다음을 직접 실행해 확인:
  1. 마이그레이션: `~/.omm/src` 없는 상태에서 `omm update` → clone +
     editable 설치 성공, `omm scan` 등 기존 명령이 정상 동작.
  2. fast path, 변경 없음: 재실행 시 "already up to date" 즉시 출력.
  3. fast path, 변경 있음: 리모트에 더미 커밋 하나 push(또는 로컬 clone
     HEAD를 한 커밋 뒤로 되돌린 뒤) `omm update` → git reset --hard로
     반영되고 `omm`이 새 코드로 동작.
  4. 손상 케이스: `~/.omm/src/.git` 지운 뒤 `omm update` → 재migration.
