"""omm CLI entry point (apt/brew-style command routing)."""

import typer
from rich.console import Console
from rich.table import Table

from omm.hardware import scan_hardware

app = typer.Typer(
    name="omm",
    help="Open source Model Manager - package manager for local LLMs (GGUF).",
    no_args_is_help=True,
)
console = Console()


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


@app.command()
def update() -> None:
    """Fetch the latest recommendation rules and model index."""
    console.print("[yellow]update: not implemented yet (Phase 4)[/yellow]")


@app.command()
def recommend() -> None:
    """Scan hardware and suggest a model to install."""
    console.print("[yellow]recommend: not implemented yet (Phase 4)[/yellow]")


@app.command()
def install(model_name: str) -> None:
    """Download a model and link it into installed engines."""
    console.print(f"[yellow]install {model_name}: not implemented yet (Phase 2/3)[/yellow]")


@app.command()
def remove(model_name: str) -> None:
    """Remove a model and clean up all symlinks/manifests."""
    console.print(f"[yellow]remove {model_name}: not implemented yet (Phase 3)[/yellow]")


@app.command(name="list")
def list_models() -> None:
    """Show models installed via omm and their linked status."""
    console.print("[yellow]list: not implemented yet (Phase 2/3)[/yellow]")


if __name__ == "__main__":
    app()
