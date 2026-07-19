from rich.console import Console
from rich.progress import Progress
from typer.testing import CliRunner

from omm import cli

runner = CliRunner()


class _FakeProc:
    def __init__(self, lines, returncode=0):
        self.stdout = iter(lines)
        self._returncode = returncode

    def wait(self):
        return self._returncode


def test_install_spec_uses_bare_repo_url_on_darwin(monkeypatch):
    monkeypatch.setattr(cli.platform, "system", lambda: "Darwin")

    assert cli._install_spec() == cli.REPO_URL


def test_install_spec_adds_nvidia_extra_on_non_darwin(monkeypatch):
    monkeypatch.setattr(cli.platform, "system", lambda: "Linux")

    assert cli._install_spec() == f"omm[nvidia] @ {cli.REPO_URL}"


def test_upgrade_reinstalls_via_pipx_then_refreshes_data(monkeypatch):
    monkeypatch.setattr(cli.platform, "system", lambda: "Darwin")
    calls = []

    def fake_popen(args, **kwargs):
        calls.append(args)
        return _FakeProc(["creating virtual environment...\n", "done!\n"])

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)
    refresh_calls = []
    monkeypatch.setattr(cli, "_refresh_data", lambda: refresh_calls.append(1))

    result = runner.invoke(cli.app, ["upgrade"])

    assert result.exit_code == 0, result.stdout
    assert calls == [["pipx", "install", "--force", cli.REPO_URL]]
    assert refresh_calls == [1]
    assert "reinstalled" in result.stdout.lower()


def test_upgrade_reports_error_when_pipx_missing(monkeypatch):
    def _raise(*args, **kwargs):
        raise FileNotFoundError("pipx")

    monkeypatch.setattr(cli.subprocess, "Popen", _raise)
    refresh_calls = []
    monkeypatch.setattr(cli, "_refresh_data", lambda: refresh_calls.append(1))

    result = runner.invoke(cli.app, ["upgrade"])

    assert result.exit_code == 1
    assert "pipx not found" in result.stdout
    assert refresh_calls == []


def test_upgrade_reports_error_and_skips_data_refresh_on_pipx_failure(monkeypatch):
    monkeypatch.setattr(
        cli.subprocess,
        "Popen",
        lambda args, **kwargs: _FakeProc(["boom\n"], returncode=1),
    )
    refresh_calls = []
    monkeypatch.setattr(cli, "_refresh_data", lambda: refresh_calls.append(1))

    result = runner.invoke(cli.app, ["upgrade"])

    assert result.exit_code == 1
    assert "boom" in result.stdout
    assert refresh_calls == []


def test_run_pipx_install_advances_progress_on_known_stage_lines(monkeypatch):
    lines = [
        "creating virtual environment...\n",
        "determining package name from 'x'...\n",
        "some unrelated pip chatter\n",
        "installing omm from spec 'x'...\n",
        "done! ✨\n",
        "installed package omm 0.1.0\n",
    ]
    monkeypatch.setattr(cli.subprocess, "Popen", lambda args, **kwargs: _FakeProc(lines))

    with Progress(console=Console(quiet=True)) as progress:
        task_id = progress.add_task("upgrade", total=len(cli._PIPX_INSTALL_STAGES))
        result = cli._run_pipx_install(["pipx", "install"], progress, task_id)
        completed = progress.tasks[0].completed

    assert result.returncode == 0
    assert completed == len(cli._PIPX_INSTALL_STAGES)


def test_run_pipx_install_stalls_at_last_reached_stage_when_lines_missing(monkeypatch):
    lines = ["creating virtual environment...\n", "done! ✨\n"]
    monkeypatch.setattr(cli.subprocess, "Popen", lambda args, **kwargs: _FakeProc(lines))

    with Progress(console=Console(quiet=True)) as progress:
        task_id = progress.add_task("upgrade", total=len(cli._PIPX_INSTALL_STAGES))
        cli._run_pipx_install(["pipx", "install"], progress, task_id)
        completed = progress.tasks[0].completed

    # "creating virtual environment" (stage 1) then "done!" (stage 4) -
    # stages 2/3 never printed, so we jump straight to 4, not fabricate 2/3.
    assert completed == 4
