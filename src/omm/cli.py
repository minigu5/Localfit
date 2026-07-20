"""omm CLI entry point (apt/brew-style command routing)."""

import importlib.metadata
import json
import platform
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import questionary
import requests
import typer
from prompt_toolkit.keys import Keys
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from omm import (
    benchmark,
    benchmark_history,
    linker,
    predictor,
    registry,
    rules as rules_mod,
    scan_import,
    search as search_mod,
    session_cache,
    telemetry,
    version_check,
)
from omm import contribute as contribute_mod
from omm.completion import complete_install_name, complete_remove_filename
from omm.config import MODELS_DIR, load_config, save_config
from omm.downloader import DownloadCancelled, DownloadError, download_file
from omm.hardware import scan_hardware
from omm.hashutil import sha256_file
from omm.hub import (
    HF_DOWNLOAD,
    AmbiguousModelError,
    ModelResolutionError,
    ResolvedModel,
    rank_quant_variants,
    remote_file_sha256,
    resolve_model,
)

app = typer.Typer(
    name="omm",
    help="Open source Model Manager - package manager for local LLMs (GGUF).",
)
console = Console()

REPO_URL = "git+https://github.com/minigu5/Localfit.git"


def _omm_version() -> str:
    try:
        return importlib.metadata.version("omm")
    except importlib.metadata.PackageNotFoundError:
        return "dev"


@app.callback(invoke_without_command=True)
def _root(ctx: typer.Context) -> None:
    _maybe_start_update_check(ctx)
    if ctx.invoked_subcommand is None:
        console.print(f"omm {_omm_version()}")
        raise typer.Exit(0)
    _maybe_auto_import(ctx)
    resent = telemetry.flush_pending()
    if resent:
        console.print(
            f"[dim]Sent {resent} queued telemetry event(s) from a previous session.[/dim]"
        )


@app.command(name="help")
def help_cmd(
    ctx: typer.Context,
    command: str = typer.Argument(None, help="Show help for a specific subcommand."),
) -> None:
    """Show help, same as --help."""
    root_ctx = ctx.find_root()
    if command is None:
        console.print(root_ctx.get_help())
        raise typer.Exit(0)

    cmd_obj = root_ctx.command.get_command(root_ctx, command)
    if cmd_obj is None:
        console.print(f"[red]No such command '{command}'. See `omm help`.[/red]")
        raise typer.Exit(1)

    sub_ctx = cmd_obj.make_context(command, [], parent=root_ctx, resilient_parsing=True)
    console.print(cmd_obj.get_help(sub_ctx))


def _install_spec() -> str:
    """NVIDIA VRAM detection is dead weight on Mac (no NVIDIA GPUs since
    2016) - only pull that extra in on other platforms, mirroring
    install.sh."""
    if platform.system() == "Darwin":
        return REPO_URL
    return f"omm[nvidia] @ {REPO_URL}"


@app.command()
def scan() -> None:
    """Scan current PC hardware (RAM, VRAM, OS) and print a summary table."""
    info = scan_hardware()

    table = Table(title="omm hardware scan")
    table.add_column("Field", style="cyan")
    table.add_column("Value", style="white")

    table.add_row("OS", f"{info.os_name} {info.os_version}")
    table.add_row("CPU", info.cpu)
    table.add_row("RAM (total)", f"{info.ram_total_gb:.1f} GB")
    table.add_row("RAM (available)", f"{info.ram_available_gb:.1f} GB")

    if info.unified_memory:
        table.add_row("Memory type", "Unified (Apple Silicon)")
        table.add_row("GPU", info.gpu_name or "Unknown")
    elif info.gpu_name:
        table.add_row("GPU", info.gpu_name)
        table.add_row("VRAM (total)", f"{info.vram_total_gb:.1f} GB")
        table.add_row("VRAM (free)", f"{info.vram_free_gb:.1f} GB")
    else:
        table.add_row("GPU", "None detected (no NVIDIA GPU found)")

    console.print(table)


def _refresh_data() -> None:
    """Unconditionally re-fetch rules.json and recommend-model.json from
    their configured URLs (used by `omm update` for a full data sync)."""
    config = load_config()

    rules_url = config.get("rules_url")
    if rules_url:
        try:
            fetched = rules_mod.fetch_rules(rules_url)
            console.print(f"[green]Updated rules.json ({len(fetched)} entries) from {rules_url}[/green]")
        except requests.RequestException as e:
            console.print(f"[red]Failed to fetch rules from {rules_url}: {e}[/red]")
    else:
        console.print("[dim]No rules_url configured - using bundled defaults.[/dim]")

    model_url = config.get("model_url")
    if model_url:
        try:
            artifact = predictor.fetch_and_cache_model(model_url)
            console.print(
                f"[green]Updated recommend-model.json "
                f"({len(artifact.get('candidates', []))} candidates) from {model_url}[/green]"
            )
        except (requests.RequestException, ValueError) as e:
            console.print(f"[red]Failed to fetch trained model from {model_url}: {e}[/red]")


_BARE_REPO_URL = REPO_URL.removeprefix("git+")


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


def _remote_head_commit(ref: str = "main") -> str | None:
    """Latest commit on the given ref of the omm repo, via `git ls-remote`
    (no GitHub API rate limit, no auth needed for a public repo)."""
    try:
        result = subprocess.run(
            ["git", "ls-remote", _BARE_REPO_URL, ref],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.split()[0]


def _cached_remote_head_commit(ref: str = "main") -> str | None:
    return version_check.cached_remote_head(_remote_head_commit, ref)


_SKIP_UPDATE_CHECK_SUBCOMMANDS = {"update", "help", "_bg-version-check"}


@app.command(name="_bg-version-check", hidden=True)
def _bg_version_check_cmd() -> None:
    """Internal. Spawned by `_maybe_start_update_check` as a detached child
    so the `git ls-remote` round trip survives the short-lived parent
    command exiting; writes the result to the shared cache for a later
    `omm` invocation to pick up."""
    version_check.cached_remote_head(_remote_head_commit)


def _print_update_notice(latest: str | None, installed: str) -> None:
    if latest and latest != installed:
        console.print("[yellow]Update available! Run: [bold]omm update[/bold][/yellow]")


def _maybe_start_update_check(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand in _SKIP_UPDATE_CHECK_SUBCOMMANDS:
        return
    installed = _installed_commit()
    if not installed:  # editable/dev install - nothing to compare against
        return
    fresh, latest = version_check.cached_remote_head_if_fresh()
    if fresh:
        ctx.call_on_close(lambda: _print_update_notice(latest, installed))
        return
    if version_check.should_start_check():
        version_check.mark_checking()
        try:
            subprocess.Popen(
                [sys.executable, "-m", "omm.cli", "_bg-version-check"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            pass


_SKIP_AUTO_IMPORT_SUBCOMMANDS = {"update", "help", "import", "_bg-version-check"}


def _maybe_auto_import(ctx: typer.Context) -> None:
    """One-time, best-effort offer to adopt stray .gguf files already
    sitting in Ollama's/LM Studio's own directories into the omm hub.
    Runs on the first interactive command after install (not from
    install.sh itself - curl|sh has no TTY for questionary's prompts) and
    never again once the flag is set, whether or not anything was found."""
    if ctx.invoked_subcommand in _SKIP_AUTO_IMPORT_SUBCOMMANDS:
        return
    config = load_config()
    if config.get("external_scan_done"):
        return
    if not sys.stdin.isatty():
        return
    config["external_scan_done"] = True
    save_config(config)
    _run_import_flow()


def _run_import_flow(extra_path: Path | None = None) -> None:
    found = scan_import.find_external_models(extra_path)
    groups = scan_import.group_by_hash(found)
    if not groups:
        console.print("[dim]No externally-managed .gguf files found.[/dim]")
        return

    total_gb = sum(g.size_bytes for g in groups) / (1024**3)
    console.print(
        f"Found {len(groups)} model(s) ({len(found)} file(s), ~{total_gb:.1f} GB) "
        "in Ollama/LM Studio not yet managed by omm."
    )
    if not _ask_confirm(f"Import {len(groups)} model(s) into the omm hub?"):
        console.print("[yellow]Skipped.[/yellow]")
        return

    choices = [
        questionary.Choice(
            title=f"{g.display_name} ({g.size_bytes / (1024**3):.1f} GB, found in: {', '.join(g.engines)})",
            value=g.sha256,
            checked=True,
        )
        for g in groups
    ]
    selected_hashes = _ask_select(questionary.checkbox("Select which models to import:", choices=choices))
    if not selected_hashes:
        console.print("[yellow]Nothing selected, skipped.[/yellow]")
        return

    bytes_saved = 0
    for group in groups:
        if group.sha256 not in selected_hashes:
            continue
        result = scan_import.adopt_group(group)
        bytes_saved += result.bytes_saved
        console.print(f"  [green]Imported {result.filename}[/green]")

    final_count = len(registry.load_registry())
    console.print(
        f"[bold green]Done: {final_count} model(s) in the omm hub, "
        f"{bytes_saved / (1024**3):.1f} GB saved.[/bold green]"
    )


@app.command(name="import")
def import_cmd(
    path: str = typer.Argument(
        None, help="Optional extra directory to also scan for stray .gguf files."
    ),
) -> None:
    """Scan Ollama/LM Studio (and optionally PATH) for .gguf files not yet
    managed by omm, and offer to adopt them into the hub."""
    extra_path = None
    if path:
        extra_path = Path(path).expanduser()
        if not extra_path.is_dir():
            console.print(f"[red]Not a directory: {extra_path}[/red]")
            raise typer.Exit(1)
    _run_import_flow(extra_path)


# pipx gives no byte-level install progress, but it does print a fixed,
# ordered sequence of stage lines to stdout - use those as real (if coarse)
# progress checkpoints instead of an indeterminate animation that never
# actually reflects how far along the install is.
_PIPX_INSTALL_STAGES = [
    "creating virtual environment",
    "determining package name",
    "installing omm from spec",
    "done!",
    "installed package",
]


def _run_pipx_install(args: list[str], progress: Progress, task_id) -> subprocess.CompletedProcess:
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    output_lines: list[str] = []
    stage = 0
    for line in proc.stdout:
        output_lines.append(line)
        lowered = line.lower()
        for i in range(stage, len(_PIPX_INSTALL_STAGES)):
            if _PIPX_INSTALL_STAGES[i] in lowered:
                stage = i + 1
                progress.update(task_id, completed=stage)
                break
    returncode = proc.wait()
    output = "".join(output_lines)
    return subprocess.CompletedProcess(args, returncode, stdout=output, stderr=output)


@app.command()
def update() -> None:
    """Reinstall omm from the latest source via pipx, then refresh rules/model data."""
    installed = _installed_commit()
    latest = _remote_head_commit() if installed else None
    if latest:
        version_check.record(latest)
    if installed and latest and installed == latest:
        console.print(f"[green]omm is already up to date ({installed[:7]}).[/green]")
        _refresh_data()
        return

    console.print(f"Updating omm from {REPO_URL} ...")
    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[cyan]Reinstalling omm via pipx...[/cyan]"),
            BarColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            task_id = progress.add_task("upgrade", total=len(_PIPX_INSTALL_STAGES))
            result = _run_pipx_install(
                ["pipx", "install", "--force", _install_spec()], progress, task_id
            )
            progress.update(task_id, completed=len(_PIPX_INSTALL_STAGES))
    except FileNotFoundError:
        console.print(
            "[red]pipx not found. Install it first, or rerun the installer:[/red]\n"
            "  curl -fsSL https://raw.githubusercontent.com/minigu5/Localfit/main/install.sh | sh"
        )
        raise typer.Exit(1)

    if result.returncode != 0:
        console.print(f"[red]pipx install failed:[/red]\n{result.stderr}")
        raise typer.Exit(1)

    console.print("[green]omm reinstalled from the latest source.[/green]")
    _refresh_data()


def _add_escape_to_cancel(question: questionary.Question) -> questionary.Question:
    """questionary only aborts on Ctrl+C/Ctrl+Q by default; make Escape do
    the same so `.ask()` returns None instead of requiring Ctrl+C."""

    def _abort(event) -> None:
        event.app.exit(exception=KeyboardInterrupt, style="class:aborting")

    question.application.key_bindings.add(Keys.Escape, eager=True)(_abort)
    return question


def _ask_select(question: questionary.Question):
    return _add_escape_to_cancel(question).ask()


def _ask_confirm(message: str, default: bool = False) -> bool:
    """Yes/no prompt that answers on the y/n keypress itself (no Enter
    needed) via questionary's auto_enter. questionary.confirm's internal
    key bindings are already merged by the time we get the Question object,
    so (unlike _ask_select) we can't bolt an Escape binding on here -
    Ctrl+C/Ctrl+Q still cancel via questionary's own bindings."""
    answer = questionary.confirm(message, default=default, auto_enter=True).ask()
    return bool(answer)


@app.command()
def recommend() -> None:
    """Scan hardware and suggest a model to install, ranked by a model
    trained on real install telemetry (falls back to static rules if the
    trained model can't be fetched)."""
    info = scan_hardware()
    config = load_config()

    artifact, changed = predictor.load_model_with_change_note(config.get("model_url"))
    if changed:
        console.print("[dim]Fetched updated recommendation data from GitHub.[/dim]")
    if artifact and artifact.get("candidates"):
        ranked = predictor.rank_candidates(artifact, info)
        viable = [(c, speed) for c, speed in ranked if speed > 0][:10]
        if not viable:
            console.print("[red]No model is predicted to run on this hardware.[/red]")
            raise typer.Exit(1)

        refs = [f"{c['repo_id']}:{c['filename']}" for c, speed in viable]
        session_cache.record_seen(refs)
        choices = [
            questionary.Choice(
                title=f"{c['name']} (~{speed:.0f} tok/s predicted) - {c.get('description', '')}",
                value=ref,
            )
            for (c, speed), ref in zip(viable, refs)
        ]
        selected = _ask_select(questionary.select("Pick a model to install:", choices=choices))
        if selected is None:
            console.print("[yellow]Cancelled.[/yellow]")
            raise typer.Exit(0)
        install(selected)
        return

    console.print("[dim]No trained model available, falling back to static rules.[/dim]")
    rules_url = config.get("rules_url")
    if rules_url:
        try:
            _, rules_changed = rules_mod.refresh_rules_with_change_note(rules_url)
            if rules_changed:
                console.print("[dim]Fetched updated rules from GitHub.[/dim]")
        except requests.RequestException:
            pass

    has_gpu = info.vram_total_gb is not None
    available_gb = info.vram_total_gb if has_gpu else info.ram_total_gb

    rule_list = rules_mod.load_rules()
    matches = rules_mod.matching_rules(rule_list, available_gb, has_gpu=has_gpu)

    if not matches:
        console.print("[red]No model in the current rules fits this hardware.[/red]")
        raise typer.Exit(1)

    session_cache.record_seen([r["name"] for r in matches])
    choices = [
        questionary.Choice(title=f"{r['name']} - {r['description']}", value=r["name"])
        for r in matches
    ]
    selected = _ask_select(questionary.select("Pick a model to install:", choices=choices))
    if selected is None:
        console.print("[yellow]Cancelled.[/yellow]")
        raise typer.Exit(0)

    install(selected)


def _resolve_ref(arg: str) -> str:
    """If `arg` is a bare integer, treat it as a 1-based index into the last
    `omm search`/`omm list` results shown in this terminal. Any non-numeric
    arg passes through unchanged."""
    if not arg.isdigit():
        return arg

    results = session_cache.load_last_results()
    if not results:
        console.print(
            "[red]Run `omm search` or `omm list` first to install/uninstall by number.[/red]"
        )
        raise typer.Exit(1)

    idx = int(arg)
    if idx < 1 or idx > len(results):
        console.print(f"[red]No result #{idx} (1-{len(results)}).[/red]")
        raise typer.Exit(1)

    return results[idx - 1]


def _pick_quant_variant(error: AmbiguousModelError) -> str | None:
    """Rank the ambiguous repo's .gguf files by fit against this PC's RAM/VRAM
    and let the user pick one, cursor defaulted to the best-fitting, highest
    quality option."""
    info = scan_hardware()
    has_gpu = info.vram_total_gb is not None
    available_gb = info.vram_total_gb if has_gpu else info.ram_total_gb

    variants = rank_quant_variants(error.candidates, available_gb, error.param_count_b)
    choices = []
    for v in variants:
        if v.fits is True:
            note = f"fits, ~{v.required_gb:.1f}GB needed"
        elif v.fits is False:
            note = f"may not fit, ~{v.required_gb:.1f}GB needed (you have {available_gb:.1f}GB)"
        else:
            note = "fit unknown"
        choices.append(questionary.Choice(title=f"{v.filename}  ({note})", value=v.filename))

    return _ask_select(
        questionary.select(f"Select a quantization variant for '{error.repo_id}':", choices=choices)
    )


def _link_model(dest, repo_id: str | None, ollama_tag: str) -> dict[str, bool]:
    """Link a downloaded .gguf into LM Studio/Ollama, printing a skip
    notice for whichever engine isn't installed or fails to link. Shared
    by `install` and `update` since both need the exact same behavior
    after a fresh (or refreshed) download."""
    linked = {"lmstudio": False, "ollama": False}

    if linker.is_lmstudio_installed():
        try:
            linker.link_lmstudio(dest, repo_id)
            linked["lmstudio"] = True
        except linker.LinkError as e:
            console.print(f"[yellow]LM Studio link skipped: {e}[/yellow]")
    else:
        console.print("[dim]LM Studio not detected, skipping link.[/dim]")

    if linker.is_ollama_installed():
        try:
            has_chat_template = linker.link_ollama(dest, ollama_tag)
            linked["ollama"] = True
            if not has_chat_template:
                console.print(
                    "[yellow]This GGUF has no embedded chat template - "
                    "Ollama will fall back to raw completion (no chat formatting).[/yellow]"
                )
        except linker.LinkError as e:
            console.print(f"[yellow]Ollama link skipped: {e}[/yellow]")
    else:
        console.print("[dim]Ollama not detected, skipping link.[/dim]")

    return linked


@dataclass
class InstallOutcome:
    filename: str
    repo_id: str | None
    linked: dict[str, bool]
    ollama_tag: str | None = None
    tokens_per_sec: float | None = None
    telemetry_sent: bool = False
    skipped_unfit: bool = False
    sha256: str | None = None


class ContributionStopped(Exception):
    """Esc fired mid-download or mid-benchmark inside `_install_impl`
    while running under `omm contribute`."""

    def __init__(self, filename: str) -> None:
        super().__init__(filename)
        self.filename = filename


class _Interrupted(Exception):
    pass


def _run_interruptible(fn, stop_event: threading.Event | None):
    """Run `fn()`, but if `stop_event` fires while it's in flight, return
    control (raising `_Interrupted`) instead of blocking until `fn`
    finishes. With no `stop_event`, just calls `fn()` directly - no thread
    pool overhead on the plain `omm install` path."""
    if stop_event is None:
        return fn()

    from concurrent.futures import ThreadPoolExecutor
    from concurrent.futures import TimeoutError as _FuturesTimeoutError

    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(fn)
        while True:
            if stop_event.is_set():
                raise _Interrupted()
            try:
                return future.result(timeout=0.2)
            except _FuturesTimeoutError:
                continue
    finally:
        pool.shutdown(wait=False)


def _install_impl(
    resolved,
    *,
    auto_benchmark: bool = False,
    skip_unfit: bool = False,
    stop_event: threading.Event | None = None,
) -> InstallOutcome:
    """Core of `omm install`: download, link, register, optionally
    benchmark+report telemetry. Shared by the plain `install` command and
    `omm contribute`'s unattended loop via the kwargs above."""
    url, filename, repo_id = resolved.url, resolved.filename, resolved.repo_id

    artifact = predictor.load_cached_model()
    trees = artifact.get("trees") if artifact else None
    if trees is not None:
        hw = scan_hardware()
        speed = predictor.predict_speed(trees, hw, {"repo_id": repo_id, "filename": filename})
        if speed <= 0:
            console.print(
                f"[red]Warning: this hardware is predicted not to run {filename}.[/red]"
            )
            if skip_unfit:
                return InstallOutcome(filename, repo_id, linked={}, skipped_unfit=True)
            if not _ask_confirm("Install anyway?"):
                console.print("[yellow]Cancelled.[/yellow]")
                raise typer.Exit(0)

    dest = MODELS_DIR / filename
    if dest.exists():
        console.print(f"[yellow]{filename} already downloaded, skipping fetch.[/yellow]")
    else:
        try:
            if stop_event is not None:
                download_file(url, dest, stop_check=stop_event.is_set)
            else:
                download_file(url, dest)
        except DownloadCancelled as e:
            raise ContributionStopped(filename) from e
        except DownloadError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1) from e

    console.print("Verifying checksum...")
    sha256 = sha256_file(dest)

    ollama_tag = linker.sanitize_ollama_tag(filename)
    linked = _link_model(dest, repo_id, ollama_tag)

    registry.upsert_entry(
        filename,
        sha256=sha256,
        version=sha256[:7],
        source=url,
        size_bytes=dest.stat().st_size,
        installed_at=datetime.now(timezone.utc).isoformat(),
        ollama_name=ollama_tag,
        repo_id=repo_id,
        linked=linked,
    )

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

    return InstallOutcome(
        filename, repo_id, linked, ollama_tag, tokens_per_sec, telemetry_sent, sha256=sha256
    )


@app.command()
def install(
    model_name: str = typer.Argument(..., autocompletion=complete_install_name),
) -> None:
    """Download a model into the central hub and link it into installed engines."""
    model_name = _resolve_ref(model_name)
    try:
        resolved = resolve_model(model_name)
    except AmbiguousModelError as e:
        chosen = _pick_quant_variant(e)
        if chosen is None:
            console.print("[yellow]Cancelled.[/yellow]")
            raise typer.Exit(0)
        install(f"{e.repo_id}:{chosen}")
        return
    except ModelResolutionError as e:
        console.print(f"[red]{e}[/red]")
        _print_install_suggestions(model_name)
        raise typer.Exit(1) from e

    outcome = _install_impl(resolved)

    console.print(f"[green]Installed {outcome.filename}[/green]")
    if outcome.linked.get("ollama"):
        console.print(f"  Ollama: [green]ollama run {outcome.ollama_tag}[/green]")
    if outcome.linked.get("lmstudio"):
        console.print("  LM Studio: visible in your local models list")
    console.print(f"  Uninstall with: [cyan]omm uninstall {outcome.filename}[/cyan]")


def _cleanup_incomplete_install(filename: str) -> bool:
    dest = MODELS_DIR / filename
    part = dest.with_suffix(dest.suffix + ".part")
    cleaned = False
    if part.exists():
        part.unlink()
        cleaned = True
    if dest.exists():
        dest.unlink()
        cleaned = True
    return cleaned


def _remove_one(filename: str, entry: dict) -> None:
    linked = entry.get("linked", {})
    if linked.get("lmstudio"):
        linker.unlink_lmstudio(filename, entry.get("repo_id"))
    if linked.get("ollama"):
        linker.unlink_ollama(entry.get("ollama_name", linker.sanitize_ollama_tag(filename)))

    dest = MODELS_DIR / filename
    dest.unlink(missing_ok=True)
    dest.with_suffix(dest.suffix + ".part").unlink(missing_ok=True)

    registry.remove_entry(filename)
    console.print(f"[green]Removed {filename}[/green]")


@app.command(name="uninstall")
def remove(
    filename: str = typer.Argument(..., autocompletion=complete_remove_filename),
) -> None:
    """Uninstall a model and clean up all symlinks/manifests. Pass `all` to
    uninstall every model installed via omm."""
    if filename.lower() == "all":
        reg = registry.load_registry()
        if not reg:
            console.print("No models installed via omm yet.")
            raise typer.Exit(0)
        if not _ask_confirm(f"Uninstall all {len(reg)} model(s)?"):
            console.print("[yellow]Cancelled.[/yellow]")
            raise typer.Exit(0)
        for name, entry in list(reg.items()):
            _remove_one(name, entry)
        return

    filename = _resolve_ref(filename)
    reg = registry.load_registry()
    entry = reg.get(filename)
    if entry is None and not filename.lower().endswith(".gguf"):
        filename = f"{filename}.gguf"
        entry = reg.get(filename)
    if entry is None:
        if _cleanup_incomplete_install(filename):
            console.print(f"[green]Cleaned up incomplete install of {filename}[/green]")
            raise typer.Exit(0)
        console.print(f"[red]{filename} is not installed via omm. See `omm list`.[/red]")
        raise typer.Exit(1)

    _remove_one(filename, entry)


def _lookup_entry(filename: str, reg: dict) -> tuple[str, dict] | tuple[None, None]:
    """Find a registry entry by exact filename, retrying with a `.gguf`
    suffix appended (mirrors the lookup `remove` already does)."""
    entry = reg.get(filename)
    if entry is None and not filename.lower().endswith(".gguf"):
        filename = f"{filename}.gguf"
        entry = reg.get(filename)
    if entry is None:
        return None, None
    return filename, entry


def _entry_version(entry: dict) -> str:
    return entry.get("version") or (entry.get("sha256") or "")[:7] or "unknown"


@app.command()
def info(
    model_name: str = typer.Argument(..., autocompletion=complete_remove_filename),
) -> None:
    """Show name, version, size, and linked-program run commands for an installed model."""
    model_name = _resolve_ref(model_name)
    reg = registry.load_registry()
    filename, entry = _lookup_entry(model_name, reg)
    if entry is None:
        console.print(f"[red]{model_name} is not installed via omm. See `omm list`.[/red]")
        raise typer.Exit(1)

    size_gb = entry.get("size_bytes", 0) / (1024**3)
    linked = entry.get("linked", {})

    table = Table(title=filename, show_header=False)
    table.add_column("Field", style="cyan")
    table.add_column("Value")
    table.add_row("Repo", entry.get("repo_id") or "(direct URL install)")
    table.add_row("Version", _entry_version(entry))
    table.add_row("Size", f"{size_gb:.2f} GB")
    table.add_row("Installed at", entry.get("installed_at", "unknown"))
    table.add_row(
        "LM Studio",
        "linked (visible in LM Studio app)" if linked.get("lmstudio") else "not linked",
    )
    if linked.get("ollama"):
        ollama_tag = entry.get("ollama_name") or linker.sanitize_ollama_tag(filename)
        table.add_row("Ollama", f"ollama run {ollama_tag}")
    else:
        table.add_row("Ollama", "not linked")

    console.print(table)


def _update_one(filename: str, entry: dict) -> str:
    """Refresh one installed model against its source. Returns "updated",
    "up_to_date", or "skipped". HF-repo installs check a cheap remote hash
    first and only re-download on a mismatch; direct-URL installs have no
    such endpoint, so they re-download to a temp file and compare hashes
    before swapping it in."""
    dest = MODELS_DIR / filename
    repo_id = entry.get("repo_id")
    old_sha256 = entry.get("sha256")

    if repo_id:
        remote_sha256 = remote_file_sha256(repo_id, filename)
        if remote_sha256 is None:
            console.print(
                f"[yellow]{filename}: could not check for updates "
                "(no repo/LFS info), skipped.[/yellow]"
            )
            return "skipped"
        if remote_sha256 == old_sha256:
            return "up_to_date"

        url = HF_DOWNLOAD.format(repo_id=repo_id, filename=filename)
        try:
            download_file(url, dest)
        except DownloadError as e:
            console.print(f"[red]{filename}: update download failed: {e}[/red]")
            return "skipped"
        new_sha256 = sha256_file(dest)
    else:
        source = entry.get("source")
        if not source:
            console.print(f"[yellow]{filename}: no source URL on record, skipped.[/yellow]")
            return "skipped"

        tmp = dest.with_name(dest.name + ".update")
        try:
            download_file(source, tmp)
        except DownloadError as e:
            console.print(f"[red]{filename}: update download failed: {e}[/red]")
            tmp.unlink(missing_ok=True)
            tmp.with_suffix(tmp.suffix + ".part").unlink(missing_ok=True)
            return "skipped"

        new_sha256 = sha256_file(tmp)
        if new_sha256 == old_sha256:
            tmp.unlink(missing_ok=True)
            return "up_to_date"
        tmp.replace(dest)

    ollama_tag = entry.get("ollama_name") or linker.sanitize_ollama_tag(filename)
    linked = _link_model(dest, repo_id, ollama_tag)
    registry.upsert_entry(
        filename,
        sha256=new_sha256,
        version=new_sha256[:7],
        size_bytes=dest.stat().st_size,
        installed_at=datetime.now(timezone.utc).isoformat(),
        ollama_name=ollama_tag,
        linked=linked,
    )
    return "updated"


@app.command()
def upgrade(
    model_name: str = typer.Argument(None, autocompletion=complete_remove_filename),
) -> None:
    """Refresh an installed model against its source, re-downloading only
    if the source has changed since install. With no argument (or `all`),
    checks every model installed via omm."""
    reg = registry.load_registry()

    if model_name is None or model_name.lower() == "all":
        if not reg:
            console.print("No models installed via omm yet.")
            raise typer.Exit(0)
        if not _ask_confirm(f"Check {len(reg)} model(s) for updates?"):
            console.print("[yellow]Cancelled.[/yellow]")
            raise typer.Exit(0)

        counts = {"updated": 0, "up_to_date": 0, "skipped": 0}
        for filename, entry in list(reg.items()):
            counts[_update_one(filename, entry)] += 1
        console.print(
            f"[green]{counts['updated']} updated, {counts['up_to_date']} up to date, "
            f"{counts['skipped']} skipped.[/green]"
        )
        return

    resolved = _resolve_ref(model_name)
    filename, entry = _lookup_entry(resolved, reg)
    if entry is None:
        console.print(f"[red]{resolved} is not installed via omm. See `omm list`.[/red]")
        raise typer.Exit(1)

    result = _update_one(filename, entry)
    if result == "up_to_date":
        console.print(f"[green]{filename} is already up to date ({_entry_version(entry)}).[/green]")
    elif result == "updated":
        fresh_entry = registry.load_registry()[filename]
        console.print(f"[green]{filename} updated to {_entry_version(fresh_entry)}.[/green]")


@app.command(name="list")
def list_models() -> None:
    """Show models installed via omm and their linked status."""
    reg = registry.load_registry()
    if not reg:
        console.print("No models installed via omm yet. Try `omm recommend` or `omm install`.")
        raise typer.Exit(0)

    table = Table(title="omm models")
    table.add_column("#", justify="right")
    table.add_column("Filename", style="cyan")
    table.add_column("Size", justify="right")
    table.add_column("LM Studio")
    table.add_column("Ollama")

    for idx, (filename, entry) in enumerate(reg.items(), start=1):
        size_gb = entry.get("size_bytes", 0) / (1024**3)
        linked = entry.get("linked", {})
        table.add_row(
            str(idx),
            filename,
            f"{size_gb:.2f} GB",
            "[green]yes[/green]" if linked.get("lmstudio") else "no",
            "[green]yes[/green]" if linked.get("ollama") else "no",
        )
    console.print(table)
    session_cache.record_results(list(reg.keys()))


@app.command()
def search(query: str) -> None:
    """Search curated models, cached candidates, and HuggingFace by name."""
    config = load_config()
    pool = search_mod.local_candidate_pool(config.get("model_url"))
    local_matches = search_mod.match_candidates(pool, query)

    local_repo_ids = {c.get("repo_id") for c in local_matches if c.get("repo_id")}
    hf_matches = [
        c
        for c in search_mod.search_huggingface(query)
        if c.get("repo_id") not in local_repo_ids
    ]

    combined = search_mod.dedupe_by_base_repo(local_matches + hf_matches)
    if not combined:
        console.print(f"[yellow]No models found matching '{query}'.[/yellow]")
        raise typer.Exit(1)

    # No network call here - only score against whatever's already cached
    # locally, same as install completion.
    artifact = predictor.load_cached_model()
    trees = artifact.get("trees") if artifact else None
    hw = scan_hardware() if trees else None

    groups = search_mod.group_by_family(combined)
    refs: list[str] = []
    seen_refs: set[str] = set()
    for family in sorted(groups):
        console.print(f"[bold cyan]==> {family}[/bold cyan]")
        for c in groups[family]:
            ref = search_mod.install_ref(c)
            if ref in seen_refs:
                continue
            seen_refs.add(ref)
            refs.append(ref)
            desc = c.get("description") or ""
            if trees is not None and predictor.predict_speed(trees, hw, c) <= 0:
                console.print(f"  [{len(refs)}] [red]{ref}  (predicted not to run on this hardware)[/red]")
            else:
                console.print(f"  [{len(refs)}] {ref}  [dim]{desc}[/dim]")
        console.print()

    session_cache.record_results(refs)


def _print_install_suggestions(query: str) -> None:
    config = load_config()
    pool = search_mod.local_candidate_pool(config.get("model_url"))
    suggestions = search_mod.dedupe_by_base_repo(search_mod.suggest_similar(query, pool, limit=3))

    existing_labels = {s.get("name") or s.get("repo_id") for s in suggestions}
    if len(suggestions) < 3:
        for hit in search_mod.search_huggingface(query, limit=5):
            if len(suggestions) >= 3:
                break
            label = hit.get("name") or hit.get("repo_id")
            if label in existing_labels:
                continue
            suggestions.append(hit)
            existing_labels.add(label)

    if not suggestions:
        return

    console.print("[yellow]Did you mean one of these?[/yellow]")
    for s in suggestions:
        console.print(f"  - {search_mod.install_ref(s)}")


@app.command()
def relink() -> None:
    """Re-verify every installed model's LM Studio/Ollama links and repair
    them. Covers models that were never linked *and* ones whose link is now
    broken, missing, or stale - link_lmstudio/link_ollama always replace the
    existing symlink/manifest, so this always re-links rather than trusting
    the registry's stored `linked` flag."""
    reg = registry.load_registry()
    if not reg:
        console.print("No models installed via omm yet.")
        raise typer.Exit(0)

    lmstudio_installed = linker.is_lmstudio_installed()
    ollama_installed = linker.is_ollama_installed()

    relinked_count = 0
    skipped_missing = 0

    for filename, entry in reg.items():
        dest = MODELS_DIR / filename
        if not dest.exists():
            skipped_missing += 1
            continue

        new_linked: dict[str, bool] = {}
        update_fields: dict[str, str] = {}
        changed = False

        if lmstudio_installed:
            try:
                linker.link_lmstudio(dest, entry.get("repo_id"))
                new_linked["lmstudio"] = True
                changed = True
            except linker.LinkError as e:
                console.print(f"[yellow]{filename}: LM Studio link skipped: {e}[/yellow]")

        if ollama_installed:
            ollama_tag = entry.get("ollama_name") or linker.sanitize_ollama_tag(filename)
            try:
                linker.link_ollama(dest, ollama_tag)
                new_linked["ollama"] = True
                update_fields["ollama_name"] = ollama_tag
                changed = True
            except linker.LinkError as e:
                console.print(f"[yellow]{filename}: Ollama link skipped: {e}[/yellow]")

        if changed:
            registry.upsert_entry(filename, linked=new_linked, **update_fields)
            relinked_count += 1

    console.print(
        f"[green]{relinked_count} model(s) relinked/verified.[/green] "
        f"{skipped_missing} skipped (file missing)."
    )


def _autoremove_incomplete_installs() -> int:
    if not MODELS_DIR.exists():
        return 0

    reg = registry.load_registry()
    removed = 0
    for path in MODELS_DIR.iterdir():
        if not path.is_file():
            continue
        if path.suffix == ".part":
            if path.with_suffix("").name not in reg:
                path.unlink()
                removed += 1
        elif path.suffix == ".gguf" and path.name not in reg:
            path.unlink()
            removed += 1
    return removed


@app.command()
def autoremove() -> None:
    """Remove broken symlinks left behind when a model's source .gguf was
    deleted without going through `omm uninstall`, plus any orphaned partial or
    unregistered downloads in the models directory."""
    lmstudio_removed = linker.autoremove_lmstudio() if linker.is_lmstudio_installed() else 0
    ollama_blobs_removed, ollama_manifests_removed = (
        linker.autoremove_ollama() if linker.is_ollama_installed() else (0, 0)
    )
    incomplete_removed = _autoremove_incomplete_installs()

    if lmstudio_removed == 0 and ollama_blobs_removed == 0 and incomplete_removed == 0:
        console.print("[green]No broken symlinks found.[/green]")
        return

    console.print(
        f"[green]Removed {lmstudio_removed} broken LM Studio symlink(s) and "
        f"{ollama_blobs_removed} broken Ollama blob(s) "
        f"({ollama_manifests_removed} manifest(s) cleaned up), "
        f"{incomplete_removed} incomplete install file(s) cleaned up.[/green]"
    )


def _report_telemetry(filename: str, repo_id: str | None, tokens_per_sec: float | None) -> bool:
    if tokens_per_sec is None:
        # Ollama daemon wasn't reachable - not a real "it doesn't run" signal,
        # so skip rather than polluting the speed-regression training data.
        telemetry.log_attempt("skipped_daemon_unreachable", filename)
        console.print(
            "[dim]Telemetry not sent - Ollama daemon wasn't reachable during benchmark.[/dim]"
        )
        return False
    info = scan_hardware()
    sent = telemetry.send_event(
        {
            "os": info.os_name,
            "cpu": info.cpu,
            "gpu": info.gpu_name,
            "ram_gb": round(info.ram_total_gb, 1),
            "vram_gb": round(info.vram_total_gb, 1) if info.vram_total_gb is not None else None,
            "unified_memory": info.unified_memory,
            "model_installed": filename,
            "model_repo_id": repo_id,
            "engine": "ollama",
            "tokens_per_sec": round(tokens_per_sec, 2),
        },
        force=True,
    )
    if not sent:
        console.print("[dim]Telemetry not sent (will retry next time you run omm).[/dim]")
    return sent


@dataclass
class _ContributionStats:
    benchmarked: list[tuple[str, float]]
    skipped_unfit: int = 0
    attempted_not_uploaded: int = 0


def _telemetry_row_count(endpoint: str) -> int | None:
    """Best-effort read of how many rows exist in the (read-open) Firebase
    telemetry endpoint, for `omm contribute`'s before/after summary."""
    try:
        resp = requests.get(f"{endpoint}?shallow=true", timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return len(data) if isinstance(data, dict) else 0
    except (requests.RequestException, ValueError):
        return None


class _EscListener:
    """Background key-listener so Esc can interrupt `omm contribute` even
    mid-download/mid-benchmark, not just at a questionary prompt. No-ops
    (Ctrl+C is still the fallback) when stdin isn't a real terminal - tests,
    CI, and piped input all fall into this path, mirroring session_cache.py's
    tty-detection idiom."""

    def __init__(self) -> None:
        self.stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        try:
            import os

            os.ttyname(0)
        except OSError:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            import select

            from prompt_toolkit.input import create_input

            inp = create_input()
            with inp.raw_mode():
                while not self.stop_event.is_set():
                    ready, _, _ = select.select([inp.fileno()], [], [], 0.1)
                    if not ready:
                        continue
                    for key_press in inp.read_keys():
                        if key_press.key == Keys.Escape:
                            self.stop_event.set()
        except Exception:
            pass  # best-effort; Ctrl+C still works as a fallback


def _run_contribution_loop(queue, stop_event: threading.Event, refetch) -> _ContributionStats:
    stats = _ContributionStats(benchmarked=[])
    while not stop_event.is_set():
        candidate = queue.next_candidate(refetch=refetch)
        if candidate is None:
            console.print("[dim]No more candidates available for this hardware.[/dim]")
            break

        resolved = ResolvedModel(
            url=HF_DOWNLOAD.format(repo_id=candidate["repo_id"], filename=candidate["filename"]),
            filename=candidate["filename"],
            repo_id=candidate["repo_id"],
        )
        display_name = candidate.get("name", candidate["filename"])
        console.print(f"[cyan]Trying {display_name}...[/cyan]")

        try:
            outcome = _install_impl(
                resolved, auto_benchmark=True, skip_unfit=True, stop_event=stop_event
            )
        except ContributionStopped as e:
            _cleanup_incomplete_install(e.filename)
            reg = registry.load_registry()
            fn, entry = _lookup_entry(e.filename, reg)
            if entry:
                _remove_one(fn, entry)
            break
        except (DownloadError, linker.LinkError) as e:
            console.print(f"[yellow]Skipping {candidate['filename']}: {e}[/yellow]")
            continue

        if outcome.skipped_unfit:
            stats.skipped_unfit += 1
            continue

        reg = registry.load_registry()
        fn, entry = _lookup_entry(outcome.filename, reg)
        if entry:
            _remove_one(fn, entry)

        if outcome.tokens_per_sec is not None and outcome.telemetry_sent:
            ref_str = contribute_mod.ref(candidate)
            benchmark_history.record_benchmarked(
                ref_str,
                repo_id=outcome.repo_id,
                filename=outcome.filename,
                sha256=outcome.sha256 or "",
                tokens_per_sec=outcome.tokens_per_sec,
            )
            queue.mark_seen(ref_str)
            stats.benchmarked.append((display_name, outcome.tokens_per_sec))
        else:
            stats.attempted_not_uploaded += 1

    return stats


def _print_contribution_summary(
    stats: _ContributionStats,
    duration_seconds: float,
    before_count: int | None,
    after_count: int | None,
) -> None:
    minutes, seconds = divmod(int(duration_seconds), 60)
    console.print("=" * 70)
    console.print("[bold]omm contribute: session summary[/bold]")
    console.print(f"Duration: {minutes}m {seconds}s")
    console.print(f"Models benchmarked+uploaded: {len(stats.benchmarked)}")
    for name, tokens_per_sec in stats.benchmarked:
        console.print(f"  - {name:<40} {tokens_per_sec:.1f} tok/s")
    console.print(f"Skipped (predicted not to fit this hardware): {stats.skipped_unfit}")
    console.print(f"Attempted but not uploaded (kept for retry): {stats.attempted_not_uploaded}")
    if before_count is not None and after_count is not None:
        console.print(
            f"Global telemetry dataset: {before_count} -> {after_count} rows "
            f"({after_count - before_count:+d})"
        )
        console.print(
            "  [dim](delta may include uploads from other contributors during this session)[/dim]"
        )
    console.print("=" * 70)


@app.command()
def contribute() -> None:
    """Repeatedly install, benchmark, and upload telemetry for hardware-fit
    models until Esc is pressed, to help grow the training dataset behind
    `omm recommend`. Deletes each model after benchmarking it (even
    successful ones) to keep disk usage bounded."""
    console.print(
        "[yellow]This will repeatedly download, benchmark, and delete GGUF models "
        "until you press Esc. It uses real bandwidth, disk space, and compute, "
        "and runs unattended (no per-model confirmation).[/yellow]"
    )
    if not _ask_confirm("Start contributing compute now?"):
        console.print("[yellow]Cancelled.[/yellow]")
        raise typer.Exit(0)

    if not benchmark.ollama_daemon_reachable():
        console.print(
            "[red]omm contribute requires a running Ollama daemon - "
            "it's the only benchmarkable engine right now.[/red]"
        )
        raise typer.Exit(1)

    config = load_config()
    artifact, _ = predictor.load_model_with_change_note(config.get("model_url"))
    if not artifact or not artifact.get("candidates"):
        console.print(
            "[red]No trained recommendation model available - can't select candidates.[/red]"
        )
        raise typer.Exit(1)

    endpoint = config.get("telemetry_endpoint")
    before_count = _telemetry_row_count(endpoint) if endpoint else None

    hw = scan_hardware()
    history_refs = benchmark_history.loaded_refs()
    queue = contribute_mod.ContributionQueue(artifact, hw, history_refs)

    def refetch():
        return predictor.load_model_with_change_note(config.get("model_url"))

    listener = _EscListener()
    listener.start()
    start_time = time.monotonic()
    try:
        stats = _run_contribution_loop(queue, listener.stop_event, refetch)
    finally:
        listener.stop_event.set()

    autoremove()

    after_count = _telemetry_row_count(endpoint) if endpoint else None
    duration = time.monotonic() - start_time
    _print_contribution_summary(stats, duration, before_count, after_count)


if __name__ == "__main__":
    app()
