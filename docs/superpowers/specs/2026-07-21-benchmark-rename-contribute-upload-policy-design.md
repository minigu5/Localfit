# `omm quality-eval` → `omm benchmark` 개편 + 업로드 정책 3단계화

## 배경

[[project-omm]]. 지금 `omm quality-eval`(`cli.py:1621`)은 이미 설치된 Ollama
태그들에 대해 정답률 pack(`quality.py`)과 median-of-N 속도를 재서 로컬 JSON에만
저장하고, 서버로는 절대 보내지 않는다(`tests/test_cli_quality_eval.py`의
`test_quality_eval_saves_local_report_and_never_uploads`가 그 계약을 고정해둠).

한편 `omm install`/`omm contribute`가 쓰는 기본 벤치마크(`benchmark.py`의
`benchmark_ollama`)는 단발 측정만 하고 텔레메트리 서버(`localfit_server`, CI
학습 스크립트 `scripts/train_model.py`가 읽는 그 서버)로 속도만 보낸다. 정답률
데이터는 서버에 전혀 안 쌓인다.

이번 변경은 다음을 합친다:

1. `omm quality-eval` → `omm benchmark`로 개명 (더 직관적인 이름).
2. `omm install`은 지금처럼 가벼운 단발 벤치마크만 유지.
3. `omm contribute`는 후보 모델마다 새 `omm benchmark`(정답률+median-of-N)를
   돌려서 서버에 정답률까지 함께 업로드 — "나중에 모델 재학습에 정답률도 넣을 수
   있게" 하기 위함.
4. `omm benchmark`를 단독 실행했을 때도 끝나면 서버 전송 여부를 물어봄
   (지금까지의 "로컬 전용" 계약을 깬다 — 위 테스트 이름도 바뀐다).
5. 서버 전송을 매번 물어볼지/항상 보낼지/항상 안 보낼지 `omm setting telemetry`
   에서 고르게 한다. 지금 있는 `telemetry_opt_in`(bool)은 실제로는 아무것도
   안 걸러서(`telemetry.py:100-104`, 유일한 호출부인 `_report_telemetry`가 항상
   `force=True`로 우회) 죽은 필드나 다름없다 — 이번에 진짜로 동작하는 3단계
   값으로 교체한다.

## 1. `omm quality-eval` → `omm benchmark`

- `cli.py:1621` `@app.command(name="quality-eval")` → `@app.command(name="benchmark")`.
- `cli.py:1622` `def quality_eval_cmd(...)` → `def benchmark_cmd(...)`. 인자
  (`models`, `--pack`, `--output`, `--speed-runs`, `--json`)는 그대로.
- `README.md:21`, `README.md:49`의 `omm quality-eval` 표기를 `omm benchmark`로
  갱신. `docs/validation-evidence-2026-07-20.md`와 기존 스펙 파일들은 그 시점의
  기록이므로 건드리지 않는다.
- `src/omm/quality.py` 모듈/함수명(`QualityEvaluationError`, `collect_evidence`
  등)은 그대로 — 명령어 이름만 바뀌는 것이고 내부 구현 모듈명은 유지.

## 2. `omm install` — 변경 없음

`_install_impl`의 벤치마크 단계(`cli.py:850-854`, `benchmark.benchmark_ollama`
단발 호출)는 그대로 둔다. `use_quality_eval` 같은 스위치를 넣지 않고, install
경로는 아예 손대지 않는다 — 가벼운 속도 유지가 목적이므로 분기 자체를 추가하지
않는 편이 코드도 더 단순하다.

## 3. `omm contribute` — 후보마다 `omm benchmark` 사용

`_run_contribution_loop`(`cli.py:1786`)이 각 후보에 대해 `_install_impl`을
호출하는 지점(`cli.py:1803`)을 새 전용 경로로 분리한다. `_install_impl` 자체를
분기 파라미터로 오염시키지 않고, contribute 전용 헬퍼를 새로 만든다:

```python
def _install_and_evaluate_for_contribution(
    resolved: ResolvedModel,
    pack: dict,
    stop_event: threading.Event,
) -> InstallOutcome:
    """omm contribute 전용: 설치 후 omm benchmark와 동일한 evaluate_model()로
    정답률+median-of-N 속도를 재서 그대로 서버에 업로드한다. 확인 없음 —
    contribute 시작 시 이미 세션 전체에 대한 동의를 받았다."""
```

- 다운로드/링크/레지스트리 등록까지는 `_install_impl`의 앞부분과 동일하게
  수행해야 하므로, `_install_impl`을 "다운로드+링크까지"와 "벤치마크+업로드"
  두 조각으로 쪼갠다:
  - `_download_and_link(resolved, *, skip_unfit: bool, stop_event) -> InstallOutcome | None`
    — 지금 `_install_impl`의 fit 예측(786~812행)부터 레지스트리 등록(835~845행)
    까지 그대로 이동. `skip_unfit`이고 예측이 안 맞으면 `linked={}` 등으로 채운
    `InstallOutcome(..., skipped_unfit=True)`를 반환(호출자가 바로 return하면
    됨). 그 외엔 `(filename, repo_id, linked, ollama_tag, sha256)`를 담은 값
    객체(작은 dataclass, 예: `_LinkedModel`)를 반환.
  - `omm install` 경로: `_install_impl`이 `_download_and_link` 다음 기존
    `benchmark_ollama` 단발 측정 + `_resolve_upload_decision` 확인(4번 항목)을
    그대로 수행 — `omm install`은 지금처럼 `_install_impl` 하나만 호출.
  - `omm contribute` 경로: `_run_contribution_loop`가 `_install_impl` 대신
    `_download_and_link` 직접 호출 후 `quality_mod.evaluate_model(ollama_tag,
    pack, speed_runs=3)`를 돌리는 새 헬퍼
    `_install_and_evaluate_for_contribution`을 호출.
- pack은 루프 시작 전 `quality_mod.load_pack()`으로 한 번만 읽어서
  (`_run_contribution_loop` 진입 시) 매 후보마다 파일을 다시 읽지 않는다.
- `evaluate_model` 결과에서:
  - `tokens_per_sec = result["speed"]["median_tokens_per_sec"]`
  - `speed_min = min(result["speed"]["samples_tokens_per_sec"])`,
    `speed_max = max(...)`, `sample_count = result["speed"]["runs"]`
  - `quality = {"pack_id": pack["pack_id"], "pack_version": pack.get("pack_version"),
    "correct": result["quality"]["correct"], "total": result["quality"]["total"],
    "accuracy": result["quality"]["accuracy"]}`
- `_maybe_auto_calibrate`는 그대로 이 `tokens_per_sec`(median)로 호출 — 오히려
  단발보다 더 안정적인 입력이라 그대로 재사용.
- 업로드는 무조건 실행(`_report_telemetry(..., force 경로)`) — contribute는
  "Start contributing compute now?" 확인에서 이미 세션 전체 동의를 받았으므로
  후보 하나하나에 대해 다시 묻지 않는다. 정책이 `never`면애초에 9번 항목에서
  `omm contribute` 자체가 시작을 거부하므로 이 지점에 도달하지 않는다.
- `evaluate_model`이 예외(`QualityEvaluationError`)를 던지면(Ollama가 응답
  안 하거나 형식이 이상하면) 해당 후보는 `DownloadError`/`linker.LinkError`와
  같은 취급으로 스킵하고 다음 후보로 넘어간다(`_run_contribution_loop`의 기존
  `continue` 패턴).
- 후보 처리 후 `quality_mod.unload_model(ollama_tag)` 호출 — 기존
  `collect_evidence`가 하던 것과 동일하게 모델을 언로드한 뒤 기존처럼 파일
  삭제(`_remove_one`).

## 4. 업로드 정책 3단계 + 공통 헬퍼

### 4.1 config

`config.py`:

```python
DEFAULT_CONFIG: dict[str, Any] = {
    "telemetry_send_policy": "ask",  # "ask" | "always" | "never"
    ...
    "contribute_always_ack": False,
    ...
}
```

- `telemetry_opt_in` 필드는 완전히 제거(하위호환 shim 없음). 다만 이미
  `~/.omm/config.json`에 `telemetry_opt_in`이 저장돼 있는 기존 사용자의 설정을
  버리지 않도록 `_merge_config`에 1회성 마이그레이션을 추가:

```python
def _merge_config(data: dict[str, Any]) -> dict[str, Any]:
    if "telemetry_send_policy" not in data and "telemetry_opt_in" in data:
        data = {**data, "telemetry_send_policy": "always" if data["telemetry_opt_in"] else "ask"}
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

### 4.2 `telemetry.send_event`

`telemetry.py:100-104`의 게이트를 필드명 교체만:

```python
def send_event(event: dict[str, Any], force: bool = False) -> bool:
    config_data = load_config()
    if not force and config_data.get("telemetry_send_policy") != "always":
        log_attempt("skipped_opt_out")
        return False
    ...
```

### 4.3 `omm setting telemetry` (`cli.py:1183`)

```python
@setting_app.command(name="telemetry")
def configure_telemetry(
    endpoint: str = typer.Option(None, "--endpoint", help="..."),
    enable: bool = typer.Option(False, "--enable", help="Always send benchmark results without asking."),
    disable: bool = typer.Option(False, "--disable", help="Never send benchmark results."),
    ask: bool = typer.Option(False, "--ask", help="Ask every time (default)."),
) -> None:
    chosen = [flag for flag in (enable, disable, ask) if flag]
    if len(chosen) > 1:
        console.print("[red]Choose only one of --enable, --disable, or --ask.[/red]")
        raise typer.Exit(1)
    current = load_config()
    changes = {}
    if endpoint is not None:
        ... # 기존 로직 그대로 (telemetry_endpoint / telemetry_backend)
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

- `--disable`/`--ask`는 엔드포인트 필수 아님(기존 `--enable`만 필수 유지).

### 4.4 `setting_menu` (`cli.py:1375-1394`)

"Uploads" 선택지에 `Ask every time`(value=`"ask"`) 추가, `Leave unchanged`는
그대로 두되 액션 매핑에 `ask` 분기 추가:

```python
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

### 4.5 공통 결정 헬퍼

`_ask_confirm` 근처(`cli.py:448` 부근)에 추가:

```python
def _resolve_upload_decision(prompt: str) -> bool:
    policy = load_config().get("telemetry_send_policy", "ask")
    if policy == "always":
        return True
    if policy == "never":
        return False
    return _ask_confirm(prompt)
```

- `omm install`의 `_install_impl` 내부 (기존 `cli.py:862-864`)
  `want_upload = auto_upload or _ask_confirm(...)` →
  `want_upload = auto_upload or _resolve_upload_decision("Send this machine's benchmark result to the server?")`.
- 새 `omm benchmark` 끝부분(5번 항목)도 이 헬퍼 사용.

## 5. `omm benchmark` 끝에 업로드 확인 추가

`benchmark_cmd`(옛 `quality_eval_cmd`, `cli.py:1621` 이하) 마지막, 테이블/로컬
저장 메시지 출력 이후:

```python
if _resolve_upload_decision(
    "Send these benchmark results to the server to help train the recommendation model?"
):
    hw = scan_hardware()
    reg = registry.load_registry()
    for model in report["models"]:
        entry = next((e for e in reg.values() if e.get("ollama_name") == model["tag"]), None)
        _report_telemetry(
            model["tag"],
            entry.get("repo_id") if entry else None,
            model["speed"]["median_tokens_per_sec"],
            size_bytes=model.get("size_bytes"),
            sample_count=model["speed"]["runs"],
            speed_min=min(model["speed"]["samples_tokens_per_sec"]),
            speed_max=max(model["speed"]["samples_tokens_per_sec"]),
            quality={
                "pack_id": report["pack"]["id"],
                "pack_version": report["pack"]["version"],
                "correct": model["quality"]["correct"],
                "total": model["quality"]["total"],
                "accuracy": model["quality"]["accuracy"],
            },
        )
```

- `_report_telemetry` 시그니처 확장(`cli.py:1691`):

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
        ...  # 기존 그대로
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
    ...  # 기존 그대로
```

- 기존 install 호출부(`_report_telemetry(filename, repo_id, tokens_per_sec)`,
  `cli.py:866`/`870`)는 새 키워드 인자를 안 써도 그대로 동작(전부 기본값).
- contribute 경로(3번 항목)는 `sample_count`/`speed_min`/`speed_max`/`quality`를
  채워서 호출.

## 6. 서버 스키마 (`src/localfit_server/app.py`)

`BenchmarkEvent`에 optional 필드 5개 추가:

```python
quality_pack_id: str | None = Field(default=None, max_length=100)
quality_pack_version: str | None = Field(default=None, max_length=20)
quality_correct: int | None = Field(default=None, ge=0, le=100)
quality_total: int | None = Field(default=None, ge=1, le=100)
quality_accuracy: float | None = Field(default=None, ge=0, le=1)
```

- 전부 optional. `model_validator`에 all-or-nothing 검증 추가 (기존
  `tokens_per_sec_min/max` 패턴과 동일):

```python
@model_validator(mode="after")
def validate_quality_summary(self) -> "BenchmarkEvent":
    quality_fields = (
        self.quality_pack_id, self.quality_pack_version,
        self.quality_correct, self.quality_total, self.quality_accuracy,
    )
    if any(f is not None for f in quality_fields) and any(f is None for f in quality_fields):
        raise ValueError("quality fields must all be supplied together")
    if self.quality_correct is not None and self.quality_total is not None:
        if self.quality_correct > self.quality_total:
            raise ValueError("quality_correct cannot exceed quality_total")
    return self
```

- `db.py` 변경 없음 — `export()`가 이미 `event_json` 전체를 반환하므로 새 필드는
  SQL 컬럼 없이도 그대로 보존된다. 카테고리별 breakdown은 안 보낸다(총계만,
  브레인스토밍에서 확정).
- `scripts/train_model.py`는 이번 변경 범위 밖 — 정답률을 학습에 실제로 쓰는 건
  나중 작업. 지금은 서버에 데이터만 쌓아둔다.

## 7. `omm contribute` + 정책 게이트

`contribute()`(`cli.py:1869` 이하) 맨 앞, 기존 디스크/대역폭 경고보다 먼저:

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
        "and future `omm contribute` runs will be sent to the server without "
        "asking each time.[/yellow]"
    )
    if not _ask_confirm("Continue?"):
        console.print("[yellow]Cancelled.[/yellow]")
        raise typer.Exit(0)
    update_config(contribute_always_ack=True)
```

- 기존 "Start contributing compute now?" 경고 문구(`cli.py:1875-1879`)에 업로드
  자동 전송 문구 보강:

```python
console.print(
    "[yellow]This will repeatedly download, benchmark, and delete GGUF models "
    "until you press Esc. It uses real bandwidth, disk space, and compute, "
    "runs unattended (no per-model confirmation), and uploads every benchmark "
    "result to the server per your current upload policy "
    f"({policy}).[/yellow]"
)
```

- `policy == "ask"`(기본값)일 때는 이 문구 보강 외에 추가 게이트 없음 — 세션
  시작 confirm 한 번이 곧 세션 전체 업로드 동의.

## 8. 영향받지 않는 것

- `omm quality-eval`이 쓰던 로컬 evidence 저장(`quality_mod.write_evidence`,
  `EVALUATIONS_DIR`)은 그대로 — 이번 변경은 로컬 저장에 "서버 전송도 선택 가능"을
  추가하는 것이지 대체하는 게 아니다.
- `quality.py`의 pack 검증/속도 측정 로직 자체는 변경 없음.
- `scripts/train_model.py`, `.github/workflows/train.yml`은 변경 없음(6번 항목
  참고).

## 9. 테스트 변경

- `tests/test_cli_quality_eval.py` → `tests/test_cli_benchmark.py`로 이동,
  `["quality-eval", ...]` → `["benchmark", ...]`.
  `test_quality_eval_saves_local_report_and_never_uploads`는 계약이 바뀌므로
  제거하고 다음으로 교체:
  - `test_benchmark_saves_local_report_and_asks_before_upload` (정책 `ask`,
    confirm에 no 응답 → `send_event` 호출 안 됨, 로컬 저장은 됨)
  - `test_benchmark_uploads_when_confirmed`
  - `test_benchmark_never_uploads_when_policy_never`(confirm 자체가 안 뜸)
  - `test_benchmark_uploads_without_confirm_when_policy_always`
- `tests/test_cli_telemetry_config.py`: `telemetry_opt_in` 어서션들을
  `telemetry_send_policy` 값(`"always"`/`"never"`/`"ask"`)으로 교체. `--ask`
  옵션과 3개 동시 지정 시 에러 케이스 추가.
- `tests/test_config_migration.py`: 기존 `telemetry_opt_in: true/false` 저장된
  config를 로드했을 때 `telemetry_send_policy`가 `always`/`ask`로 승계되고
  `telemetry_opt_in` 키 자체는 사라지는 케이스 추가.
- `tests/test_telemetry.py`: `send_event`의 게이트 조건을
  `telemetry_send_policy == "always"` 기준으로 갱신.
- `tests/test_contribute_loop.py` / `tests/test_cli_contribute.py`:
  - 후보 처리 시 `benchmark.benchmark_ollama`가 아니라
    `quality_mod.evaluate_model`이 호출되는지로 교체.
  - `telemetry_send_policy == "never"`면 `omm contribute`가 exit_code 1로
    거부하는 케이스 추가.
  - `telemetry_send_policy == "always"`이고 `contribute_always_ack`가 없으면
    경고+confirm 뜨고, 거절 시 exit_code 0, 수락 시 `contribute_always_ack`가
    `True`로 저장되고 루프가 진행되는 케이스 추가.
  - 두 번째 실행부터는(`contribute_always_ack: True`) 경고 없이 바로 진행되는
    케이스 추가.
- `tests/test_self_hosted_server.py`: `quality_*` 5개 필드가 optional로
  들어오는 케이스, all-or-nothing 검증 위반 케이스(`quality_correct`만 있고
  나머지 없음 → 422), `quality_correct > quality_total` 위반 케이스 추가.
