from __future__ import annotations

from typer.testing import CliRunner

from omm import cli
from omm.hardware import HardwareInfo

runner = CliRunner()


def _hardware() -> HardwareInfo:
    return HardwareInfo(
        os_name="macOS",
        os_version="test",
        cpu="Apple Silicon",
        ram_total_gb=24,
        ram_available_gb=10,
        unified_memory=True,
        gpu_name="Apple GPU",
        vram_total_gb=24,
        vram_free_gb=10,
    )


def test_scan_displays_live_safe_budget(monkeypatch):
    monkeypatch.setattr(cli, "scan_hardware", _hardware)

    result = runner.invoke(cli.app, ["scan"])

    assert result.exit_code == 0, result.stdout
    assert "Safe model budget now" in result.stdout
    assert "7.6 GB" in result.stdout
    assert "Reserved for apps/OS" in result.stdout


def test_tune_uses_live_budget_for_installed_model(monkeypatch):
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(
        cli.registry,
        "load_registry",
        lambda: {
            "model-7B-Q4.gguf": {
                "repo_id": "org/model-GGUF",
                "size_bytes": 4 * 1024**3,
            }
        },
    )

    result = runner.invoke(cli.app, ["tune", "model-7B-Q4.gguf"])

    assert result.exit_code == 0, result.stdout
    assert "Safe model budget now" in result.stdout
    assert "7.6 GB" in result.stdout
    assert "Context length" in result.stdout
