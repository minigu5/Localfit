from typer.testing import CliRunner

from omm import cli, search as search_mod

runner = CliRunner()


def test_install_unknown_model_prints_did_you_mean_suggestions(monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda: {"model_url": None})
    monkeypatch.setattr(
        search_mod,
        "local_candidate_pool",
        lambda model_url: [
            {"name": "tinyllama-1.1b-q4", "repo_id": "TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF"},
        ],
    )
    monkeypatch.setattr(search_mod, "search_huggingface", lambda query, **kwargs: [])

    result = runner.invoke(cli.app, ["install", "tinylama-1.1b-q4"])

    assert result.exit_code == 1
    assert "이런 모델을 찾으셨나요?" in result.stdout
    assert "tinyllama-1.1b-q4" in result.stdout


def test_install_unknown_model_with_no_suggestions_still_exits_cleanly(monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda: {"model_url": None})
    monkeypatch.setattr(search_mod, "local_candidate_pool", lambda model_url: [])
    monkeypatch.setattr(search_mod, "search_huggingface", lambda query, **kwargs: [])

    result = runner.invoke(cli.app, ["install", "totally-unrelated-xyz"])

    assert result.exit_code == 1
    assert "이런 모델을 찾으셨나요?" not in result.stdout
