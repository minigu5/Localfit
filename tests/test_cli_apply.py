from typer.testing import CliRunner

from omm import cli, linker, registry

runner = CliRunner()


def test_apply_relinks_entry_missing_lmstudio_link(isolated_omm_home, monkeypatch):
    filename = "tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf"
    dest = cli.MODELS_DIR / filename
    dest.write_bytes(b"fake-gguf")

    registry.save_registry(
        {
            filename: {
                "linked": {"lmstudio": False, "ollama": True},
                "repo_id": "TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF",
                "ollama_name": "tinyllama",
            }
        }
    )

    monkeypatch.setattr(linker, "is_lmstudio_installed", lambda: True)
    monkeypatch.setattr(linker, "is_ollama_installed", lambda: True)

    calls = []
    monkeypatch.setattr(
        linker,
        "link_lmstudio",
        lambda gguf_path, repo_id: calls.append((gguf_path, repo_id)),
    )

    result = runner.invoke(cli.app, ["apply"])

    assert result.exit_code == 0, result.stdout
    assert calls == [(dest, "TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF")]
    updated = registry.load_registry()[filename]
    assert updated["linked"]["lmstudio"] is True
    assert updated["linked"]["ollama"] is True  # untouched, stays True


def test_apply_skips_entry_whose_source_file_is_missing(isolated_omm_home, monkeypatch):
    registry.save_registry({"ghost.gguf": {"linked": {"lmstudio": False, "ollama": False}}})

    monkeypatch.setattr(linker, "is_lmstudio_installed", lambda: True)
    monkeypatch.setattr(linker, "is_ollama_installed", lambda: True)

    result = runner.invoke(cli.app, ["apply"])

    assert result.exit_code == 0, result.stdout
    assert "0 model(s) newly linked" in result.stdout
    assert "1 skipped" in result.stdout


def test_apply_with_empty_registry_reports_nothing_to_do(isolated_omm_home):
    result = runner.invoke(cli.app, ["apply"])

    assert result.exit_code == 0, result.stdout
    assert "No models installed" in result.stdout
