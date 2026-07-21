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


def _full_report():
    return {
        "schema_version": 1,
        "pack": {"id": "localfit-gsm8k-bilingual-smoke", "version": "1.1.0"},
        "models": [
            {
                "tag": "small:latest",
                "parameter_size": "1B",
                "quantization_level": "Q4_K_M",
                "size_bytes": 900_000_000,
                "quality": {"correct": 6, "total": 8, "accuracy": 0.75},
                "speed": {
                    "median_tokens_per_sec": 42.5,
                    "samples_tokens_per_sec": [41.0, 42.5, 44.0],
                    "runs": 3,
                },
            }
        ],
    }


def test_benchmark_saves_local_report_and_asks_before_upload(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli.quality_mod, "collect_evidence", lambda *a, **k: _full_report())
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: False)
    sent = []
    monkeypatch.setattr(cli.telemetry, "send_event", lambda event, force=False: sent.append(event) or True)

    result = runner.invoke(cli.app, ["benchmark", "small:latest"])

    assert result.exit_code == 0, result.stdout
    assert "6/8 (75.0%)" in result.stdout
    assert "42.5 tok/s" in result.stdout
    paths = list(config.EVALUATIONS_DIR.glob("quality-*.json"))
    assert len(paths) == 1
    assert json.loads(paths[0].read_text()) == _full_report()
    assert "leaderboard" in result.stdout
    assert sent == []


def test_benchmark_uploads_when_confirmed(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli.quality_mod, "collect_evidence", lambda *a, **k: _full_report())
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: True)
    sent = []
    monkeypatch.setattr(cli.telemetry, "send_event", lambda event, force=False: sent.append(event) or True)

    result = runner.invoke(cli.app, ["benchmark", "small:latest"])

    assert result.exit_code == 0, result.stdout
    assert len(sent) == 1
    event = sent[0]
    assert event["model_installed"] == "small:latest"
    assert event["model_size_bytes"] == 900_000_000
    assert event["sample_count"] == 3
    assert event["tokens_per_sec_min"] == 41.0
    assert event["tokens_per_sec_max"] == 44.0
    assert event["quality_pack_id"] == "localfit-gsm8k-bilingual-smoke"
    assert event["quality_correct"] == 6
    assert event["quality_total"] == 8
    assert event["quality_accuracy"] == 0.75


def test_benchmark_never_uploads_when_policy_never(isolated_omm_home, monkeypatch):
    config.update_config(telemetry_send_policy="never")
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli.quality_mod, "collect_evidence", lambda *a, **k: _full_report())
    monkeypatch.setattr(
        cli, "_ask_confirm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no prompt"))
    )
    sent = []
    monkeypatch.setattr(cli.telemetry, "send_event", lambda event, force=False: sent.append(event) or True)

    result = runner.invoke(cli.app, ["benchmark", "small:latest"])

    assert result.exit_code == 0, result.stdout
    assert sent == []


def test_benchmark_uploads_without_confirm_when_policy_always(isolated_omm_home, monkeypatch):
    config.update_config(telemetry_send_policy="always")
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(cli, "scan_hardware", _hardware)
    monkeypatch.setattr(cli.quality_mod, "collect_evidence", lambda *a, **k: _full_report())
    monkeypatch.setattr(
        cli, "_ask_confirm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no prompt"))
    )
    sent = []
    monkeypatch.setattr(cli.telemetry, "send_event", lambda event, force=False: sent.append(event) or True)

    result = runner.invoke(cli.app, ["benchmark", "small:latest"])

    assert result.exit_code == 0, result.stdout
    assert len(sent) == 1


def test_benchmark_stops_when_ollama_is_not_running(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: False)

    result = runner.invoke(cli.app, ["benchmark", "small:latest"])

    assert result.exit_code == 1
    assert "Ollama is not running" in result.stdout
