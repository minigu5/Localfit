import subprocess

from typer.testing import CliRunner

from omm import cli

runner = CliRunner()


def test_upgrade_reinstalls_via_pipx_then_refreshes_data(monkeypatch):
    calls = []
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda args, **kwargs: calls.append(args)
        or subprocess.CompletedProcess(args, returncode=0, stdout="", stderr=""),
    )
    update_calls = []
    monkeypatch.setattr(cli, "update", lambda: update_calls.append(1))

    result = runner.invoke(cli.app, ["upgrade"])

    assert result.exit_code == 0, result.stdout
    assert calls == [["pipx", "install", "--force", cli.REPO_URL]]
    assert update_calls == [1]
    assert "reinstalled" in result.stdout.lower()


def test_upgrade_reports_error_when_pipx_missing(monkeypatch):
    def _raise(*args, **kwargs):
        raise FileNotFoundError("pipx")

    monkeypatch.setattr(cli.subprocess, "run", _raise)
    update_calls = []
    monkeypatch.setattr(cli, "update", lambda: update_calls.append(1))

    result = runner.invoke(cli.app, ["upgrade"])

    assert result.exit_code == 1
    assert "pipx not found" in result.stdout
    assert update_calls == []


def test_upgrade_reports_error_and_skips_data_refresh_on_pipx_failure(monkeypatch):
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args, returncode=1, stdout="", stderr="boom"
        ),
    )
    update_calls = []
    monkeypatch.setattr(cli, "update", lambda: update_calls.append(1))

    result = runner.invoke(cli.app, ["upgrade"])

    assert result.exit_code == 1
    assert "boom" in result.stdout
    assert update_calls == []
