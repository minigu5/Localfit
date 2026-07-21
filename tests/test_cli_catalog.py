from typer.testing import CliRunner

from omm import catalog, cli, config

runner = CliRunner()


def test_catalog_trust_saves_verified_public_key(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(catalog, "public_key_fingerprint", lambda key: "abcd1234")

    result = runner.invoke(
        cli.app,
        ["setting", "catalog-trust", "--manifest-url", "https://example.com/manifest.json", "--public-key", "key"],
    )

    assert result.exit_code == 0, result.stdout
    saved = config.load_config()
    assert saved["catalog_manifest_url"] == "https://example.com/manifest.json"
    assert saved["catalog_public_key"] == "key"
