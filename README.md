# omm — Open source Model Manager

`omm` is an apt/brew-style package manager for local LLMs (GGUF). It installs models into a central hub, links them into LM Studio and Ollama automatically, and can recommend a model that fits your hardware.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/minigu5/Localfit/main/install.sh | sh
```

This bootstraps `python3`, `git`, and `pipx` if missing (Debian/Ubuntu via `apt`, or Homebrew on macOS), then installs `omm` as an isolated CLI via `pipx`. Open a new shell afterward so your `PATH` picks up `omm`.

Requirements: Python 3.10+. GPU detection extras (`omm[nvidia]`) are installed automatically on non-macOS platforms.

## Usage

```sh
omm scan             # Print a hardware summary (RAM, VRAM, OS)
omm recommend        # Suggest a model that fits this machine, then offer to install it
omm tune <name>      # Recommend context, GPU offload, threads, and batch size
omm quality-eval qwen3:4b exaone3.5:2.4b  # Local quality + speed smoke evidence
omm search <query>   # Search curated models, cached candidates, and HuggingFace
omm install <name>   # Download a model and link it into LM Studio / Ollama
omm uninstall <name> # Uninstall a model and clean up its symlinks/manifests
omm uninstall all    # Uninstall every model installed via omm
omm list             # Show models installed via omm and their linked status
omm info <name>      # Show a model's name, version, size, and linked-program run commands
omm upgrade <name>   # Refresh a model against its source if it has changed since install
omm upgrade          # Check every installed model for updates
omm link             # Re-verify and repair every installed model's LM Studio/Ollama links
omm link <directory> # Reuse central GGUF files in another app without copying them
omm calibrate        # Locally correct predicted speed with an installed Ollama model
omm ui compact       # Use short everyday tables (`omm ui detailed` for diagnostics)
omm catalog-status   # Show signed recommendation data and rollback snapshots
omm autoremove       # Clean up broken symlinks and orphaned partial downloads
omm update           # Reinstall omm from the latest source, then refresh rules/model data
omm help [command]   # Show help, same as --help
```

`install`, `uninstall`, `info`, and `upgrade` accept either a model name/reference or the numeric index shown by the last `omm search` or `omm list` run in that terminal. `search`/`install` mark models predicted not to run on this machine's hardware in red.

Localfit does not assume all installed memory belongs to the model. A live
scan subtracts memory currently used by other applications, keeps at least
2 GB (or 10% of RAM) for the OS and newly opened apps, and applies total-memory
caps. Recommendation fit and `omm tune` use this safe budget, so rerunning a
command adapts after memory-heavy applications are opened or closed.

`omm quality-eval` runs a versioned eight-item bilingual arithmetic smoke pack
against models already installed in Ollama. It stores parsed answers,
correctness, pinned model metadata, and fixed-length timings under
`~/.omm/evaluations/`; it stores no generated text or raw hardware names and
never uploads. The pack is intentionally small and is not a leaderboard.

## Self-hosted benchmark data

Benchmark uploads are disabled and have no server endpoint by default. To run
the bundled FastAPI + SQLite collector locally:

```sh
pip install -e ".[server]"
export LOCALFIT_DB_PATH="$PWD/localfit.db"
export LOCALFIT_ADMIN_TOKEN="replace-with-a-long-random-token"
localfit-server
```

Explicitly configure the endpoint and opt in before uploading:

```sh
omm telemetry --endpoint http://127.0.0.1:8000/v1/benchmarks --enable
```

Training can consume the authenticated export directly:

```sh
export LOCALFIT_ADMIN_TOKEN="replace-with-a-long-random-token"
python scripts/train_model.py \
  --telemetry-url http://127.0.0.1:8000/v1/benchmarks/export
```

The old Firebase JSON endpoint remains supported only when explicitly configured.
Exact duplicate events are ignored, and raw export requires the admin token.

## Signed recommendation data

`omm catalog-trust --manifest-url <https-url> --public-key <base64-key>` enables
Ed25519 verification for future recommendation downloads. Existing artifacts are
snapshotted before replacement and `omm catalog-rollback` restores the most recent
different snapshot.

## Development

```sh
pip install -e ".[dev]"
pytest
```
