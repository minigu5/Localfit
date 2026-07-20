import threading

from typer.testing import CliRunner

from omm import cli

runner = CliRunner()


class _FakeCtx:
    def __init__(self, invoked_subcommand):
        self.invoked_subcommand = invoked_subcommand
        self.close_callbacks = []

    def call_on_close(self, fn):
        self.close_callbacks.append(fn)


class _SyncThread:
    """Runs the target synchronously inside .start(), so tests don't race
    a real background thread."""

    def __init__(self, target, args=(), daemon=None):
        self.target = target
        self.args = args
        self.daemon = daemon

    def start(self):
        self.target(*self.args)


def test_background_version_check_sets_result_when_newer(monkeypatch):
    monkeypatch.setattr(cli, "_cached_remote_head_commit", lambda ref="main": "new_sha")
    done = threading.Event()
    result = {}

    cli._background_version_check("old_sha", done, result)

    assert done.is_set()
    assert result == {"latest": "new_sha"}


def test_background_version_check_no_result_when_already_current(monkeypatch):
    monkeypatch.setattr(cli, "_cached_remote_head_commit", lambda ref="main": "same_sha")
    done = threading.Event()
    result = {}

    cli._background_version_check("same_sha", done, result)

    assert done.is_set()
    assert result == {}


def test_background_version_check_swallows_exceptions(monkeypatch):
    def _raise(ref="main"):
        raise RuntimeError("network boom")

    monkeypatch.setattr(cli, "_cached_remote_head_commit", _raise)
    done = threading.Event()
    result = {}

    cli._background_version_check("old_sha", done, result)

    assert done.is_set()
    assert result == {}


def test_print_update_notice_prints_when_ready_and_newer(monkeypatch):
    printed = []
    monkeypatch.setattr(cli.console, "print", lambda *a, **k: printed.append(a))
    done = threading.Event()
    done.set()

    cli._print_update_notice(done, {"latest": "new_sha"})

    assert printed
    assert "omm update" in printed[0][0]


def test_print_update_notice_silent_when_not_done(monkeypatch):
    printed = []
    monkeypatch.setattr(cli.console, "print", lambda *a, **k: printed.append(a))

    cli._print_update_notice(threading.Event(), {"latest": "new_sha"})

    assert printed == []


def test_print_update_notice_silent_when_no_newer_version(monkeypatch):
    printed = []
    monkeypatch.setattr(cli.console, "print", lambda *a, **k: printed.append(a))
    done = threading.Event()
    done.set()

    cli._print_update_notice(done, {})

    assert printed == []


def test_maybe_start_update_check_skips_for_update_subcommand(monkeypatch):
    monkeypatch.setattr(cli.threading, "Thread", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no thread")))
    ctx = _FakeCtx("update")

    cli._maybe_start_update_check(ctx)

    assert ctx.close_callbacks == []


def test_maybe_start_update_check_skips_for_help_subcommand(monkeypatch):
    monkeypatch.setattr(cli.threading, "Thread", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no thread")))
    ctx = _FakeCtx("help")

    cli._maybe_start_update_check(ctx)

    assert ctx.close_callbacks == []


def test_maybe_start_update_check_skips_for_dev_install(monkeypatch):
    monkeypatch.setattr(cli, "_installed_commit", lambda: None)
    monkeypatch.setattr(cli.threading, "Thread", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no thread")))
    ctx = _FakeCtx("list")

    cli._maybe_start_update_check(ctx)

    assert ctx.close_callbacks == []


def test_maybe_start_update_check_starts_thread_and_registers_close_callback(monkeypatch):
    monkeypatch.setattr(cli, "_installed_commit", lambda: "old_sha")
    monkeypatch.setattr(cli, "_cached_remote_head_commit", lambda ref="main": "new_sha")
    monkeypatch.setattr(cli.threading, "Thread", _SyncThread)
    printed = []
    monkeypatch.setattr(cli.console, "print", lambda *a, **k: printed.append(a))
    ctx = _FakeCtx("list")

    cli._maybe_start_update_check(ctx)

    assert len(ctx.close_callbacks) == 1
    ctx.close_callbacks[0]()
    assert printed
    assert "omm update" in printed[0][0]


def test_end_to_end_prints_update_available_when_newer_version_exists(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli, "_installed_commit", lambda: "old_sha")
    monkeypatch.setattr(cli, "_remote_head_commit", lambda ref="main": "new_sha")
    monkeypatch.setattr(cli.threading, "Thread", _SyncThread)

    result = runner.invoke(cli.app, ["list"])

    assert result.exit_code == 0, result.stdout
    assert "omm update" in result.stdout.lower() or "omm update" in result.stdout


def test_end_to_end_silent_when_already_up_to_date(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli, "_installed_commit", lambda: "same_sha")
    monkeypatch.setattr(cli, "_remote_head_commit", lambda ref="main": "same_sha")
    monkeypatch.setattr(cli.threading, "Thread", _SyncThread)

    result = runner.invoke(cli.app, ["list"])

    assert result.exit_code == 0, result.stdout
    assert "update available" not in result.stdout.lower()


def test_end_to_end_update_subcommand_does_not_trigger_background_check(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(
        cli.threading, "Thread", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no thread"))
    )
    monkeypatch.setattr(cli, "_installed_commit", lambda: None)
    monkeypatch.setattr(cli, "_refresh_data", lambda: None)
    monkeypatch.setattr(
        cli.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError())
    )

    result = runner.invoke(cli.app, ["update"])

    assert result.exit_code == 1, result.stdout
