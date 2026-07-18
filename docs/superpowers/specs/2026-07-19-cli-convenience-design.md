# OMM CLI 편의성 업데이트 설계

## 배경

`omm`은 Homebrew 스타일 로컬 LLM(GGUF) 패키지 매니저. 현재 `scan`, `update`,
`recommend`, `install`, `remove`, `list` 명령만 존재. 아래 6가지 편의 기능을
brew 스타일에 맞춰 추가한다.

## 1. `omm search <query>`

- 로컬 후보 풀: `hub.CURATED_INDEX`(3개) + `predictor.load_model(config["model_url"])`가
  반환하는 `candidates`(현재 27개, 오프라인 시 캐시 사용).
- 위 풀에서 이름/repo_id/설명에 대해 substring 매치, 없으면
  `difflib.get_close_matches`로 오타 허용 매치.
- 네트워크 가능하면 HuggingFace 검색 API(`GET /api/models?search=<q>&filter=gguf`)도
  병행 조회(짧은 timeout), 실패/타임아웃 시 조용히 스킵하고 로컬 결과만 표시.
- 결과를 모델 패밀리별(Llama/Mistral/Qwen/Gemma/Phi/DeepSeek/Yi/TinyLlama/Mixtral/
  기타 "Other")로 그룹핑해 `==> Llama` 같은 브루 스타일 섹션 헤더로 출력.
- 패밀리는 이름+repo_id 텍스트에서 알려진 키워드 목록을 대소문자 무시 매칭해 결정.

## 2. `omm install <name>` 실패 시 추천

- `hub.resolve_model()`이 `ModelResolutionError`를 던지면:
  - 로컬 풀에서 `difflib.get_close_matches` (n=3)
  - HF 검색 API(짧은 timeout)도 병행, 실패 시 스킵
  - 합쳐서 최대 3개를 "이런 모델을 찾으셨나요?" 형태로 표시만 함 (선택형 아님,
    자동 설치 안 함). 사용자는 표시된 정확한 이름으로 다시 `omm install` 실행.

## 3. `.gguf` 확장자 자동 인식

- `install`: `org/repo:filename` 형태에서 `filename`에 `.gguf`가 없으면 자동으로
  붙여서 HF 파일 목록과 매칭.
- `remove`: 입력한 파일명이 registry에 없고 `.gguf`로 끝나지 않으면 `.gguf`를
  붙여서 재조회.

## 4. Tab 자동완성

- Typer/Click 내장 `autocompletion` 콜백 사용, 네트워크 호출 없이 즉시 응답:
  - `install <name>`: `CURATED_INDEX` 키 + 캐시된 candidates 이름
  - `remove <filename>`: `registry.load_registry()` 키
- `install.sh`에 최초 1회 `omm --install-completion` 안내 추가.

## 5. `omm apply`

- `registry.load_registry()` 순회.
- 각 항목에 대해 `linked.lmstudio`가 False인데 현재 `linker.is_lmstudio_installed()`가
  True면 `link_lmstudio` 실행 후 registry 갱신 (ollama도 동일 로직).
- `MODELS_DIR / filename`이 실제로 없으면 skip + 경고.
- 요약 출력: 새로 연결된 개수 / 이미 최신인 개수 / skip된 개수.

## 6. `omm autoremove`

- LM Studio models 디렉토리, Ollama blobs 디렉토리를 재귀 스캔.
- symlink이면서 타깃이 존재하지 않는(깨진) 것만 삭제 대상.
- LM Studio: 깨진 symlink 삭제 후 빈 publisher/repo 상위 디렉토리 정리
  (`unlink_lmstudio`의 정리 로직 재사용).
- Ollama: 깨진 blob symlink 삭제. 해당 blob을 참조하는 manifest도 함께 찾아 삭제
  (그렇지 않으면 `ollama list`가 깨진 모델을 계속 보여줌).
- registry는 변경하지 않음 (정상적으로 `omm remove`된 항목과 무관).
- 삭제 개수 요약 출력.

## 7. `omm recommend`에서 ESC로 취소

- questionary는 기본적으로 `Ctrl+C`/`Ctrl+Q`만 abort로 바인딩, ESC는 미바인딩.
- `questionary.select(...)`가 반환하는 `Question` 객체의
  `.application.key_bindings.add(Keys.Escape, eager=True)`에 `Ctrl+C`와 동일하게
  `event.app.exit(exception=KeyboardInterrupt)` 핸들러를 추가하는 헬퍼
  (`_ask_select`)를 만들어 `recommend()` 내 두 `questionary.select().ask()` 호출을
  모두 이 헬퍼로 교체. 기존에 있던 "선택 None → Cancelled" 처리 로직 그대로 재사용.

## 새 의존성

없음. `difflib`(표준 라이브러리), Click/Typer의 completion 기능, questionary의
`Application.key_bindings`(이미 설치된 prompt_toolkit) 모두 기존 의존성 안에서
해결 가능.

## 테스트 범위

- 패밀리 파싱 함수 단위 테스트 (여러 이름 → 기대 패밀리)
- `.gguf` 확장자 정규화 단위 테스트 (install/remove 양쪽)
- `autoremove`의 깨진 symlink 탐지/정리 로직 (임시 디렉토리로 심볼릭 링크 만들어 검증)
- `apply`가 `linked=False` + 파일 존재 항목만 재연결하는지
- 네트워크 관련 코드(HF 검색)는 `requests` 모킹으로 실패/성공 케이스만 검증
