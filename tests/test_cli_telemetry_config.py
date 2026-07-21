from typer.testing import CliRunner

from omm import cli, config

runner = CliRunner()


def test_telemetry_requires_explicit_endpoint_before_opt_in(isolated_omm_home):
    result = runner.invoke(cli.app, ["setting", "telemetry", "--enable"])

    assert result.exit_code == 1
    assert config.load_config()["telemetry_opt_in"] is False


def test_telemetry_accepts_local_self_hosted_endpoint(isolated_omm_home):
    result = runner.invoke(
        cli.app,
        [
            "setting",
            "telemetry",
            "--endpoint",
            "http://127.0.0.1:8000/v1/benchmarks",
            "--enable",
        ],
    )

    assert result.exit_code == 0, result.stdout
    saved = config.load_config()
    assert saved["telemetry_backend"] == "self_hosted"
    assert saved["telemetry_opt_in"] is True
