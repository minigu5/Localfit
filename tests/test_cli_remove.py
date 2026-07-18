from typer.testing import CliRunner

from omm import cli, registry

runner = CliRunner()


def test_remove_accepts_filename_without_gguf_suffix(isolated_omm_home):
    filename = "tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf"
    dest = cli.MODELS_DIR / filename
    dest.write_bytes(b"fake-gguf")
    registry.save_registry({filename: {"linked": {"lmstudio": False, "ollama": False}}})

    result = runner.invoke(cli.app, ["remove", "tinyllama-1.1b-chat-v1.0.Q4_K_M"])

    assert result.exit_code == 0, result.stdout
    assert f"Removed {filename}" in result.stdout
    assert registry.load_registry() == {}
    assert not dest.exists()
