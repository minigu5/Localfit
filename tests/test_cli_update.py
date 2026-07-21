import subprocess

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


def test_update_reinstalls_via_pipx_then_refreshes_data(monkeypatch):
    monkeypatch.setattr(cli.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(cli, "_installed_commit", lambda: None)
    monkeypatch.setattr(cli, "_deps_satisfied", lambda: True)
    calls = []

    def fake_popen(args, **kwargs):
        calls.append(args)
        return _FakeProc(["creating virtual environment...\n", "done!\n"])

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)
    refresh_calls = []
    monkeypatch.setattr(cli, "_refresh_data", lambda: refresh_calls.append(1))

    result = runner.invoke(cli.app, ["update"])

    assert result.exit_code == 0, result.stdout
    assert calls == [["pipx", "install", "--force", "--pip-args=--no-deps", cli.REPO_URL]]
    assert refresh_calls == [1]
    assert "reinstalled" in result.stdout.lower()


def test_update_falls_back_to_full_install_when_deps_missing_after_no_deps_install(monkeypatch):
    """--no-deps skips reinstalling unchanged dependencies for speed, but if
    this commit actually added/changed a dependency, `pip check` catches the
    gap and update() must redo the install with deps included rather than
    leaving omm broken."""
    monkeypatch.setattr(cli.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(cli, "_installed_commit", lambda: None)
    monkeypatch.setattr(cli, "_deps_satisfied", lambda: False)
    calls = []

    def fake_popen(args, **kwargs):
        calls.append(args)
        return _FakeProc(["creating virtual environment...\n", "done!\n"])

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(cli, "_refresh_data", lambda: None)

    result = runner.invoke(cli.app, ["update"])

    assert result.exit_code == 0, result.stdout
    assert calls == [
        ["pipx", "install", "--force", "--pip-args=--no-deps", cli.REPO_URL],
        ["pipx", "install", "--force", cli.REPO_URL],
    ]


def test_update_reports_error_when_pipx_missing(monkeypatch):
    monkeypatch.setattr(cli, "_installed_commit", lambda: None)

    def _raise(*args, **kwargs):
        raise FileNotFoundError("pipx")

    monkeypatch.setattr(cli.subprocess, "Popen", _raise)
    refresh_calls = []
    monkeypatch.setattr(cli, "_refresh_data", lambda: refresh_calls.append(1))

    result = runner.invoke(cli.app, ["update"])

    assert result.exit_code == 1
    assert "pipx not found" in result.stdout
    assert refresh_calls == []


def test_update_reports_error_and_skips_data_refresh_on_pipx_failure(monkeypatch):
    monkeypatch.setattr(cli, "_installed_commit", lambda: None)
    monkeypatch.setattr(
        cli.subprocess,
        "Popen",
        lambda args, **kwargs: _FakeProc(["boom\n"], returncode=1),
    )
    refresh_calls = []
    monkeypatch.setattr(cli, "_refresh_data", lambda: refresh_calls.append(1))

    result = runner.invoke(cli.app, ["update"])

    assert result.exit_code == 1
    assert "boom" in result.stdout
    assert refresh_calls == []


def test_update_skips_reinstall_when_already_up_to_date(monkeypatch):
    monkeypatch.setattr(cli, "_installed_commit", lambda: "abc1234" * 5 + "abc12345")
    monkeypatch.setattr(cli, "_remote_head_commit", lambda: "abc1234" * 5 + "abc12345")
    popen_calls = []
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **k: popen_calls.append(a) or _FakeProc([]))
    refresh_calls = []
    monkeypatch.setattr(cli, "_refresh_data", lambda: refresh_calls.append(1))

    result = runner.invoke(cli.app, ["update"])

    assert result.exit_code == 0, result.stdout
    assert "up to date" in result.stdout.lower()
    assert popen_calls == []
    assert refresh_calls == [1]


def test_update_refreshes_stale_cache_with_live_remote_head(monkeypatch):
    """A background check that ran before this `update` populated
    update_check.json with a now-outdated remote head. update() fetches
    the remote head live - it must write that fresh value back into the
    cache, or the next command's background check keeps serving the
    stale pre-update reading (false "Update available") until the TTL
    expires."""
    same_commit = "abc1234" * 5 + "abc12345"
    monkeypatch.setattr(cli, "_installed_commit", lambda: same_commit)
    monkeypatch.setattr(cli, "_remote_head_commit", lambda: same_commit)
    monkeypatch.setattr(cli, "_refresh_data", lambda: None)
    recorded = []
    monkeypatch.setattr(cli.version_check, "record", lambda head: recorded.append(head))

    result = runner.invoke(cli.app, ["update"])

    assert result.exit_code == 0, result.stdout
    assert recorded == [same_commit]


def test_update_reinstalls_when_installed_commit_differs_from_remote(monkeypatch):
    monkeypatch.setattr(cli.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(cli, "_installed_commit", lambda: "old" * 13 + "old")
    monkeypatch.setattr(cli, "_remote_head_commit", lambda: "new" * 13 + "new")
    monkeypatch.setattr(cli, "_deps_satisfied", lambda: True)
    calls = []

    def fake_popen(args, **kwargs):
        calls.append(args)
        return _FakeProc(["creating virtual environment...\n", "done!\n"])

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(cli, "_refresh_data", lambda: None)

    result = runner.invoke(cli.app, ["update"])

    assert result.exit_code == 0, result.stdout
    assert calls == [["pipx", "install", "--force", "--pip-args=--no-deps", cli.REPO_URL]]
    assert "up to date" not in result.stdout.lower()


def test_installed_commit_reads_vcs_info_from_direct_url_json(monkeypatch):
    class _FakeDist:
        def read_text(self, name):
            assert name == "direct_url.json"
            return '{"url": "https://x", "vcs_info": {"commit_id": "deadbeef", "vcs": "git"}}'

    monkeypatch.setattr(cli.importlib.metadata, "distribution", lambda name: _FakeDist())

    assert cli._installed_commit() == "deadbeef"


def test_installed_commit_returns_none_for_editable_dev_install(monkeypatch):
    class _FakeDist:
        def read_text(self, name):
            return '{"dir_info": {"editable": true}, "url": "file:///repo"}'

    monkeypatch.setattr(cli.importlib.metadata, "distribution", lambda name: _FakeDist())

    assert cli._installed_commit() is None


def test_installed_commit_returns_none_when_package_not_found(monkeypatch):
    def _raise(name):
        raise cli.importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(cli.importlib.metadata, "distribution", _raise)

    assert cli._installed_commit() is None


def test_remote_head_commit_parses_git_ls_remote_output(monkeypatch):
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args, 0, stdout="abcdef1234567890\trefs/heads/main\n", stderr=""
        ),
    )

    assert cli._remote_head_commit() == "abcdef1234567890"


def test_deps_satisfied_true_when_pip_check_passes(monkeypatch):
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 0, stdout="No broken requirements found.\n"),
    )

    assert cli._deps_satisfied() is True


def test_deps_satisfied_false_when_pip_check_reports_missing_dep(monkeypatch):
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 1, stdout="omm 0.1.0 requires psutil, which is not installed.\n"),
    )

    assert cli._deps_satisfied() is False


def test_deps_satisfied_false_when_pipx_missing(monkeypatch):
    def _raise(*args, **kwargs):
        raise FileNotFoundError("pipx")

    monkeypatch.setattr(cli.subprocess, "run", _raise)

    assert cli._deps_satisfied() is False


def test_remote_head_commit_returns_none_when_git_missing(monkeypatch):
    def _raise(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(cli.subprocess, "run", _raise)

    assert cli._remote_head_commit() is None


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
