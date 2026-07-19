from typer.testing import CliRunner

from omm import cli, registry

runner = CliRunner()


def test_list_shows_index_column_and_records_session(isolated_omm_home, monkeypatch):
    registry.save_registry(
        {
            "a.gguf": {"size_bytes": 0, "linked": {"lmstudio": False, "ollama": False}},
            "b.gguf": {"size_bytes": 0, "linked": {"lmstudio": False, "ollama": True}},
        }
    )
    recorded = []
    monkeypatch.setattr(cli.session_cache, "record_results", lambda refs: recorded.append(refs))

    result = runner.invoke(cli.app, ["list"])

    assert result.exit_code == 0, result.stdout
    assert recorded == [["a.gguf", "b.gguf"]]


def test_list_empty_registry_does_not_touch_session(isolated_omm_home, monkeypatch):
    recorded = []
    monkeypatch.setattr(cli.session_cache, "record_results", lambda refs: recorded.append(refs))

    result = runner.invoke(cli.app, ["list"])

    assert result.exit_code == 0, result.stdout
    assert recorded == []
