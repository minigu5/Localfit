# OMM CLI 세션 UX / 정리 / 텔레메트리 동의 설계

## 배경

`omm`은 Homebrew 스타일 로컬 LLM(GGUF) 패키지 매니저 ([[project-omm]]). 이번
업데이트는 4가지 독립 요청을 다룬다:

1. `search`/`list`/`recommend`에서 한 번이라도 보인 모델은 이후 `install` 등에서
   Tab 자동완성 대상이 되어야 한다.
2. `install` 중단으로 남은 미완성 설치를, 같은 모델 재설치 시뿐 아니라 `remove`/
   `autoremove`로도 정리할 수 있어야 한다.
3. `org/repo:filename.gguf` 같은 긴 참조 대신 번호로 설치/삭제할 수 있어야 한다.
4. `install` 끝의 벤치마크+텔레메트리 전송을 자동 실행이 아니라 매번 y/n으로
   물어야 한다.

1번과 3번은 같은 기반(터미널 세션별 결과 캐시)을 공유한다.

## 1. 세션 캐시 (`session_cache.py`, 신규)

Tab 자동완성은 키 입력마다 새 프로세스로 떠서(Typer/Click의 `autocompletion`
콜백) 같은 CLI 프로세스 메모리를 공유하지 못한다. 파일로 상태를 넘겨야 한다.

- 세션 키: `os.ttyname(sys.stdin.fileno())`을 sha1 해시한 값. TTY를 못 얻으면
  (파이프/논인터랙티브 실행) 세션 캐시 기능 전체를 조용히 no-op 처리 — 에러를
  내지 않는다.
- 저장 위치: `~/.omm/session/<hash>.json`
- 스키마:
  ```json
  {
    "seen": ["tinyllama-1.1b-q4", "org/repo:file.gguf", ...],
    "last_results": ["org/repo:file.gguf", "org2/repo2:file2.gguf", ...]
  }
  ```
  - `seen`: 자동완성 후보 풀용. 중복 제거, 최근 항목이 앞에 오도록, 최대 50개로
    cap.
  - `last_results`: 번호 참조용. `search`/`list`를 실행할 때마다 통째로
    덮어쓴다 (직전 실행한 결과만 유효).
- API:
  - `record_seen(refs: list[str]) -> None`
  - `record_results(refs: list[str]) -> None` (내부적으로 `record_seen`도 호출)
  - `load_seen() -> list[str]`
  - `resolve_index(token: str) -> str | None` — `token`이 1-base 정수 문자열이고
    `last_results` 범위 안이면 해당 ref, 아니면 `None`.
- 파일 읽기/쓰기 어느 쪽이든 실패(권한, 손상된 JSON 등)해도 예외를 밖으로
  내지 않고 빈 상태로 취급 — 세션 캐시는 편의 기능이지 필수 경로가 아니다.

## 2. Tab 자동완성 확장

- `completion.complete_install_name()`이 `session_cache.load_seen()`도 후보에
  합친다 (기존 `CURATED_INDEX` + 캐시된 recommend candidates에 추가).
- `search()`, `list_models()`, `recommend()`의 후보 선택지 구성 시
  `session_cache.record_seen()`(`recommend`는 선택 전 전체 후보 목록) /
  `record_results()`(`search`, `list`)를 호출해 채워 넣는다.
- prefix 하나로 좁혀지면 자동 채움은 셸 표준 completion 동작 그대로 — 별도
  구현 불필요.

## 3. 번호 참조

- `search()`: 출력 각 줄 앞에 `[N]` 붙이고, 그룹 순서를 유지한 채 만든 flat
  ref 리스트를 `session_cache.record_results()`로 저장.
- `list_models()`: 테이블에 `#` 컬럼 추가(1부터), 같은 순서의 파일명 리스트를
  `record_results()`로 저장.
- `install(model_name)` / `remove(filename)`: 인자가 `str.isdigit()`이면 먼저
  `session_cache.resolve_index(model_name)`으로 치환을 시도한다.
  - 치환 성공 시 그 ref로 계속 진행.
  - `last_results`가 비어 있으면: `"번호로 설치하려면 먼저 omm search 또는 omm
    list를 실행하세요."` 출력 후 `Exit(1)`.
  - 범위 밖 숫자면: `"{N}번은 없습니다 (1-{len})."` 출력 후 `Exit(1)`.
- 순수 숫자가 아닌 인자는 지금처럼 그대로 처리 (동작 변화 없음).

## 4. `remove`/`autoremove`의 미완성 설치 정리

- `remove(filename)`: registry에 항목이 없을 때, 지금은 바로 에러. 에러 내기
  전에 `MODELS_DIR`에서 다음을 확인:
  - `filename.gguf.part` (다운로드 중단)
  - `filename` 자체가 실제 파일로 존재 (다운로드는 끝났지만 registry 기록 전에
    중단된 경우)
  둘 중 하나라도 있으면 해당 파일을 지우고 `"미완료 설치 {filename} 정리
  완료"`를 출력하고 정상 종료(0). 링크된 적이 없으므로 `linker.unlink_*` 호출은
  하지 않는다. 둘 다 없으면 기존 그대로 `"is not installed via omm"` 에러.
- `autoremove()`: 기존 깨진 심볼릭링크 정리 로직에 더해, `MODELS_DIR`를 스캔해
  - registry에 없는 `*.part` 파일
  - registry에 없는 `*.gguf` 파일
  을 찾아 삭제한다. 요약 출력에 `"N개 미완료 설치 파일 정리"` 문구를 추가한다.
  registry 자체는 손대지 않는다 (기존 원칙 유지).

## 5. 벤치마크+텔레메트리 통합 동의

- 현재 `install()`은 `linked["ollama"]`가 True면 무조건 벤치마크를 돌리고
  결과를 `_report_telemetry()`로 넘긴다. `telemetry.send_event()`는 전역
  `config["telemetry_opt_in"]`이 True일 때만 실제 전송한다 — 즉 지금은 사용자가
  모르는 새 벤치마크가 항상 실행된다.
- 변경 후: `linked["ollama"]`가 True면 `typer.confirm("모델 속도를 측정하고
  결과를 서버로 보낼까요?", default=False)`를 한 번 묻는다.
  - `n`(기본값 포함): 벤치마크 자체를 실행하지 않는다.
  - `y`: `benchmark.benchmark_ollama()` 실행 → 성공 시 콘솔에 tok/s 출력
    (지금은 측정만 하고 화면에 안 보여줬음 — 같이 고침) → `_report_telemetry()`
    호출.
- 이 프롬프트는 전역 `telemetry_opt_in` 설정과 무관하게 항상 뜬다 (설정값은
  더 이상 이 경로에서 참조하지 않음). `telemetry.send_event()`에 `force: bool =
  False` 파라미터를 추가해 `force=True`일 때 opt-in 체크를 건너뛰도록 하고,
  `_report_telemetry()`가 `force=True`로 호출한다. `telemetry_opt_in`
  설정값/필드 자체는 다른 잠재적 호출부를 위해 남겨둔다.
- 벤치마크는 Ollama 데몬이 죽어 있으면(`benchmark_ollama`가 `None` 반환) 지금과
  동일하게 텔레메트리 전송을 스킵한다 (측정 실패가 아니라 측정 불가 케이스).

## 테스트

- `session_cache.py`: TTY 없는 환경(예: pytest) 시뮬레이션 — `os.ttyname` 실패
  시 no-op 확인. `resolve_index` 정상/범위밖/빈 상태 케이스.
- `install`/`remove` 번호 인자 라우팅 (mock 세션 캐시).
- `remove`의 미완성 설치 정리 (registry 없이 `.part`/`.gguf` 파일만 있는 경우).
- `autoremove`의 신규 스캔 로직.
- `install`의 confirm 프롬프트: `y`/`n` 각각에서 `benchmark_ollama`/
  `send_event` 호출 여부를 mock으로 검증.

기존 43+ 테스트 스위트에 추가하는 형태로, 전부 `isolated_omm_home` 픽스처
재사용.
