"""Shared pytest fixtures for the omm test suite."""

from __future__ import annotations

import pytest

from omm import calibration, catalog, cli, config, predictor, registry, scan_import


@pytest.fixture
def isolated_omm_home(tmp_path, monkeypatch):
    """Redirect all of omm's ~/.omm paths into a throwaway tmp_path so
    tests never touch (or depend on) the real user home directory."""
    home = tmp_path / ".omm"
    models_dir = home / "models"

    monkeypatch.setattr(config, "OMM_HOME", home)
    monkeypatch.setattr(config, "MODELS_DIR", models_dir)
    monkeypatch.setattr(config, "CONFIG_PATH", home / "config.json")
    monkeypatch.setattr(config, "REGISTRY_PATH", home / "models.json")
    monkeypatch.setattr(config, "RULES_PATH", home / "rules.json")
    monkeypatch.setattr(config, "RECOMMEND_MODEL_PATH", home / "recommend-model.json")
    monkeypatch.setattr(config, "EVALUATIONS_DIR", home / "evaluations")
    monkeypatch.setattr(config, "CALIBRATION_PATH", home / "calibration.json")
    monkeypatch.setattr(config, "CATALOG_HISTORY_DIR", home / "catalog-history")

    monkeypatch.setattr(registry, "REGISTRY_PATH", config.REGISTRY_PATH)
    monkeypatch.setattr(cli, "MODELS_DIR", models_dir)
    monkeypatch.setattr(scan_import, "MODELS_DIR", models_dir)
    monkeypatch.setattr(predictor, "RECOMMEND_MODEL_PATH", config.RECOMMEND_MODEL_PATH)
    monkeypatch.setattr(calibration, "CALIBRATION_PATH", config.CALIBRATION_PATH)
    monkeypatch.setattr(catalog, "RECOMMEND_MODEL_PATH", config.RECOMMEND_MODEL_PATH)
    monkeypatch.setattr(catalog, "CATALOG_HISTORY_DIR", config.CATALOG_HISTORY_DIR)

    config.ensure_omm_home()
    return home
