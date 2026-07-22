from types import SimpleNamespace

from typer.testing import CliRunner

from omm import cli

runner = CliRunner()


def _fake_group(sha256="deadbeef"):
    return SimpleNamespace(
        sha256=sha256,
        display_name="model.gguf",
        size_bytes=1024**3,
        engines=["ollama"],
    )


def test_import_yes_flag_skips_prompts_without_a_tty(isolated_omm_home, monkeypatch):
    group = _fake_group()
    monkeypatch.setattr(cli.scan_import, "find_external_models", lambda extra_path=None: [object()])
    monkeypatch.setattr(cli.scan_import, "group_by_hash", lambda found: [group])
    adopted = []
    monkeypatch.setattr(
        cli.scan_import,
        "adopt_group",
        lambda g: adopted.append(g.sha256) or SimpleNamespace(filename="model.gguf", bytes_saved=0),
    )
    monkeypatch.setattr(cli.registry, "load_registry", lambda: {"model.gguf": {}})

    result = runner.invoke(cli.app, ["import", "--yes"])

    assert result.exit_code == 0, result.stdout
    assert adopted == ["deadbeef"]


def test_import_without_yes_errors_without_a_tty(isolated_omm_home, monkeypatch):
    group = _fake_group()
    monkeypatch.setattr(cli.scan_import, "find_external_models", lambda extra_path=None: [object()])
    monkeypatch.setattr(cli.scan_import, "group_by_hash", lambda found: [group])
    monkeypatch.setattr(
        cli.scan_import,
        "adopt_group",
        lambda g: (_ for _ in ()).throw(AssertionError("should not adopt")),
    )

    result = runner.invoke(cli.app, ["import"])

    assert result.exit_code == 1
