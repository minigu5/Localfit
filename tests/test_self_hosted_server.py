from __future__ import annotations

from fastapi.testclient import TestClient

from localfit_server import app as server_app


def _event():
    return {
        "ram_gb": 16,
        "vram_gb": 16,
        "unified_memory": True,
        "model_installed": "model-3b-q4.gguf",
        "model_repo_id": "org/model",
        "model_size_bytes": 2_000_000_000,
        "engine": "llama.cpp",
        "benchmark_version": 4,
        "recorded_at": "2026-07-21T00:00:00Z",
        "tokens_per_sec": 19.2,
        "sample_count": 1,
        "tokens_per_sec_min": 19.2,
        "tokens_per_sec_max": 19.2,
    }


def test_self_hosted_collector_stores_and_exports_with_admin_token(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALFIT_DB_PATH", str(tmp_path / "benchmarks.sqlite3"))
    monkeypatch.setenv("LOCALFIT_ADMIN_TOKEN", "secret")
    server_app.get_store.cache_clear()
    client = TestClient(server_app.app)

    response = client.post("/v1/benchmarks", json=_event())
    assert response.status_code == 201
    duplicate = client.post("/v1/benchmarks", json=_event())
    assert duplicate.status_code == 201
    assert duplicate.json() == {"id": response.json()["id"], "status": "duplicate"}
    assert client.get("/v1/stats").json() == {"count": 1, "engines": {"llama.cpp": 1}}
    assert client.get("/v1/benchmarks/export").status_code == 401
    export = client.get(
        "/v1/benchmarks/export", headers={"Authorization": "Bearer secret"}
    )
    assert export.status_code == 200
    assert export.json()["benchmarks"][0]["tokens_per_sec"] == 19.2
    server_app.get_store.cache_clear()


def test_self_hosted_collector_rejects_unknown_or_private_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALFIT_DB_PATH", str(tmp_path / "benchmarks.sqlite3"))
    server_app.get_store.cache_clear()
    client = TestClient(server_app.app)
    event = _event()
    event["cpu_name"] = "private raw hardware name"

    assert client.post("/v1/benchmarks", json=event).status_code == 422
    server_app.get_store.cache_clear()


def test_self_hosted_collector_rejects_inconsistent_sample_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALFIT_DB_PATH", str(tmp_path / "benchmarks.sqlite3"))
    server_app.get_store.cache_clear()
    client = TestClient(server_app.app)
    event = _event()
    event["tokens_per_sec_min"] = 30
    event["tokens_per_sec_max"] = 40

    assert client.post("/v1/benchmarks", json=event).status_code == 422
    server_app.get_store.cache_clear()


def _quality_fields():
    return {
        "quality_pack_id": "localfit-gsm8k-bilingual-smoke",
        "quality_pack_version": "1.1.0",
        "quality_correct": 6,
        "quality_total": 8,
        "quality_accuracy": 0.75,
    }


def test_self_hosted_collector_accepts_optional_quality_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALFIT_DB_PATH", str(tmp_path / "benchmarks.sqlite3"))
    server_app.get_store.cache_clear()
    client = TestClient(server_app.app)
    event = _event()
    event.update(_quality_fields())

    response = client.post("/v1/benchmarks", json=event)

    assert response.status_code == 201
    server_app.get_store.cache_clear()


def test_self_hosted_collector_rejects_partial_quality_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALFIT_DB_PATH", str(tmp_path / "benchmarks.sqlite3"))
    server_app.get_store.cache_clear()
    client = TestClient(server_app.app)
    event = _event()
    event["quality_correct"] = 6

    assert client.post("/v1/benchmarks", json=event).status_code == 422
    server_app.get_store.cache_clear()


def test_self_hosted_collector_rejects_correct_over_total(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALFIT_DB_PATH", str(tmp_path / "benchmarks.sqlite3"))
    server_app.get_store.cache_clear()
    client = TestClient(server_app.app)
    event = _event()
    event.update(_quality_fields())
    event["quality_correct"] = 9

    assert client.post("/v1/benchmarks", json=event).status_code == 422
    server_app.get_store.cache_clear()
