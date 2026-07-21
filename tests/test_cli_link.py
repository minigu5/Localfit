from typer.testing import CliRunner

from omm import cli, registry

runner = CliRunner()


def test_link_custom_directory_links_every_registered_model(isolated_omm_home, tmp_path):
    filename = "model.gguf"
    source = cli.MODELS_DIR / filename
    source.write_bytes(b"model")
    registry.save_registry({filename: {"linked": {}}})
    target = tmp_path / "custom-models"

    result = runner.invoke(cli.app, ["link", str(target)])

    assert result.exit_code == 0, result.stdout
    assert (target / filename).is_symlink()
    entry = registry.load_registry()[filename]
    assert str(target / filename) in entry["custom_links"]
