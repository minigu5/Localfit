from __future__ import annotations

import json

from omm import config


def test_unaccepted_legacy_firebase_default_migrates_to_local(isolated_omm_home):
    config.CONFIG_PATH.write_text(
        json.dumps(
            {
                "telemetry_opt_in": False,
                "telemetry_endpoint": config.LEGACY_FIREBASE_ENDPOINT,
            }
        )
    )

    loaded = config.load_config()

    assert loaded["telemetry_endpoint"] is None
    assert loaded["telemetry_backend"] == "local"


def test_explicit_legacy_firebase_opt_in_is_preserved(isolated_omm_home):
    config.CONFIG_PATH.write_text(
        json.dumps(
            {
                "telemetry_opt_in": True,
                "telemetry_endpoint": config.LEGACY_FIREBASE_ENDPOINT,
            }
        )
    )

    loaded = config.load_config()

    assert loaded["telemetry_endpoint"] == config.LEGACY_FIREBASE_ENDPOINT
    assert loaded["telemetry_backend"] == "firebase_legacy"
