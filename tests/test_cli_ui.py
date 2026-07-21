from typer.testing import CliRunner

from omm import cli, config, registry

runner = CliRunner()


def test_ui_mode_can_switch_to_detailed(isolated_omm_home):
    result = runner.invoke(cli.app, ["setting", "ui", "detailed"])

    assert result.exit_code == 0, result.stdout
    assert config.load_config()["ui_mode"] == "detailed"


def test_compact_list_collapses_engine_columns(isolated_omm_home):
    registry.save_registry(
        {"model.gguf": {"size_bytes": 1024, "linked": {"ollama": True}}}
    )

    result = runner.invoke(cli.app, ["list"])

    assert result.exit_code == 0, result.stdout
    assert "Links" in result.stdout
    assert "Ollama" in result.stdout
