from __future__ import annotations

import json

from typer.testing import CliRunner

from omm import cli, config
from omm.hardware import HardwareInfo

runner = CliRunner()


def _hardware() -> HardwareInfo:
    return HardwareInfo(
        os_name="Linux",
        os_version="",
        cpu="CPU",
        ram_total_gb=16,
        ram_available_gb=12,
        unified_memory=False,
        gpu_name=None,
        vram_total_gb=None,
        vram_free_gb=None,
    )


def test_quality_eval_saves_local_report_and_never_uploads(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    report = {
        "schema_version": 1,
        "models": [
            {
                "tag": "small:latest",
                "parameter_size": "1B",
                "quantization_level": "Q4_K_M",
                "quality": {"correct": 6, "total": 8, "accuracy": 0.75},
                "speed": {"median_tokens_per_sec": 42.5},
            }
        ],
    }
    monkeypatch.setattr(cli.quality_mod, "collect_evidence", lambda *args, **kwargs: report)

    result = runner.invoke(cli.app, ["benchmark", "small:latest"])

    assert result.exit_code == 0, result.stdout
    assert "6/8 (75.0%)" in result.stdout
    assert "42.5 tok/s" in result.stdout
    paths = list(config.EVALUATIONS_DIR.glob("quality-*.json"))
    assert len(paths) == 1
    assert json.loads(paths[0].read_text()) == report
    assert "not a leaderboard" in result.stdout


def test_benchmark_stops_when_ollama_is_not_running(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: False)

    result = runner.invoke(cli.app, ["benchmark", "small:latest"])

    assert result.exit_code == 1
    assert "Ollama is not running" in result.stdout
