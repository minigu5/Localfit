# Local validation and source notes

This note distinguishes verified behavior from planned support. It intentionally
does not include machine-specific model reports, raw generations, or hardware
names.

## Verified locally

| Area | Environment | Result | Limitation |
| --- | --- | --- | --- |
| Python CLI | Apple Silicon macOS | Full test suite passed | Other Mac generations are not represented |
| Linux package | Ubuntu ARM64 through `python:3.11-slim` | Docker image built and the full test suite passed | This does not validate Linux GPU drivers or physical engine directories |
| Firebase rules | Realtime Database Emulator, Firebase CLI 15.24.0 | Valid create, raw-name rejection, unknown/range rejection, append-only behavior, default deny, and retraining read passed | Production rules were not deployed |
| Windows | GitHub Actions job defined | Pending first remote run | No real Windows engine/link cycle has been completed |

The Firebase REST test uses `demo-localfit-default-rtdb`. Using the bare demo
project ID would activate a second rules-free emulator namespace and make
authorization assertions meaningless.

## Live memory policy

Localfit separates installed memory from memory that can safely be assigned to
a model. Each `scan`, `recommend`, and `tune` invocation:

1. reads reclaimable available RAM and free VRAM;
2. keeps at least 2 GB or 10% of RAM for the OS and other applications;
3. keeps at least 0.5 GB or 5% of dedicated VRAM;
4. retains total caps of 80% RAM and 90% dedicated VRAM;
5. lowers model fit, context length, batch size, and GPU offload when the live
   budget is smaller.

Unified-memory Macs use the RAM result once rather than double-counting it as
both RAM and VRAM. Localfit recalculates when a command starts; it does not
interrupt or unload an already-running user workload.

## Reproducible quality smoke pack

`omm quality-eval` runs eight fixed arithmetic items against models already
installed in Ollama. Four prompts are the first four GSM8K test rows and four
are Localfit Korean translations of the following rows. The command uses fixed
generation settings, measures three fixed-length decode probes after warmup,
and stores only parsed numeric answers, correctness, model metadata, and timing.

It never uploads results, stores no generated text or raw hardware names, and
unloads each runner after measurement without deleting model files. Eight items
are not statistically representative, so the result must not be described as a
general model-quality leaderboard.

Sources:

- [OpenAI GSM8K repository and citation](https://github.com/openai/grade-school-math)
- [GSM8K MIT license](https://github.com/openai/grade-school-math/blob/master/LICENSE)
- [Ollama timing fields](https://docs.ollama.com/api/usage)
- [Ollama generate and keep-alive API](https://docs.ollama.com/api/generate)
- [Firebase Local Emulator Suite](https://firebase.google.com/docs/emulator-suite/install_and_configure)
- [Docker installation requirements](https://docs.docker.com/desktop/setup/install/mac-install/)

## Reproduction

```sh
python -m pytest -q

docker build --tag localfit-test .
docker run --rm localfit-test

PATH="/opt/homebrew/opt/openjdk/bin:$PATH" \
  npx --yes firebase-tools@15.24.0 emulators:exec \
  --only database --project demo-localfit \
  "node scripts/test_firebase_rules.mjs"

omm quality-eval exaone3.5:2.4b qwen3:4b qwen3.5:9b
```
