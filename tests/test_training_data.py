from __future__ import annotations

import json

import pytest

pytest.importorskip("sklearn")

from scripts import train_model


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def _row(speed: float, **overrides) -> dict:
    row = {
        "engine": "ollama",
        "benchmark_version": 2,
        "tokens_per_sec": speed,
        "ram_gb": 16,
        "vram_gb": 8,
        "gpu_tflops": 20,
        "unified_memory": False,
        "model_installed": "model-7B-Q4.gguf",
        "model_repo_id": "org/model-7B",
        "model_size_bytes": 4 * 1024**3,
        "context_length": 4096,
        "gpu_offload_percent": 100,
        "cpu_threads": 8,
        "num_batch": 512,
    }
    row.update(overrides)
    return row


def test_repeated_configuration_is_collapsed_to_median_speed():
    X, y = train_model.real_rows_to_training_data([_row(10), _row(100), _row(20)])

    assert len(X) == 1
    assert y == [20]


def test_unknown_benchmark_schema_is_ignored():
    X, y = train_model.real_rows_to_training_data([_row(20, benchmark_version=999)])

    assert X == []
    assert y == []


def test_privacy_minimized_schema_three_is_accepted_without_names():
    row = _row(20, benchmark_version=3)
    row.pop("gpu", None)
    row.pop("cpu", None)
    row.pop("os", None)

    X, y = train_model.real_rows_to_training_data([row])

    assert len(X) == 1
    assert y == [20]


def test_multi_sample_schema_four_is_accepted():
    X, y = train_model.real_rows_to_training_data(
        [
            _row(
                20,
                benchmark_version=4,
                sample_count=3,
                tokens_per_sec_min=19,
                tokens_per_sec_max=21,
            )
        ]
    )

    assert len(X) == 1
    assert y == [20]


def test_inconsistent_schema_four_sample_summary_is_rejected():
    X, y, audit = train_model.real_rows_to_training_data_with_audit(
        [
            _row(
                20,
                benchmark_version=4,
                sample_count=3,
                tokens_per_sec_min=30,
                tokens_per_sec_max=40,
            )
        ]
    )

    assert X == [] and y == []
    assert audit["rejections"] == {"invalid_samples": 1}


def test_supported_engines_are_kept_as_distinct_training_configurations():
    rows = [
        _row(20, engine="ollama"),
        _row(21, engine="llama.cpp"),
        _row(19, engine="lmstudio"),
    ]

    X, y = train_model.real_rows_to_training_data(rows)
    llama_index = train_model.FEATURE_ORDER.index("engine_llamacpp")
    lmstudio_index = train_model.FEATURE_ORDER.index("engine_lmstudio")

    assert len(X) == 3
    assert sorted(y) == [19, 20, 21]
    assert {(features[llama_index], features[lmstudio_index]) for features in X} == {
        (0.0, 0.0),
        (1.0, 0.0),
        (0.0, 1.0),
    }


def test_training_audit_explains_rejections_and_duplicate_collapse():
    rows = [
        _row(10),
        _row(20),
        _row(30, engine="other"),
        _row(40, ram_gb="not-a-number"),
        _row(50, model_installed="unknown.gguf", model_repo_id="org/unknown"),
    ]

    X, y, audit = train_model.real_rows_to_training_data_with_audit(rows)

    assert len(X) == 1
    assert y == [15]
    assert audit == {
        "raw_rows": 5,
        "valid_rows": 2,
        "rejected_rows": 3,
        "samples_used": 2,
        "samples_capped": 0,
        "unique_configurations": 1,
        "duplicates_collapsed": 1,
        "rejections": {
            "invalid_measurement": 1,
            "unparseable_model": 1,
            "unsupported_engine": 1,
        },
    }


def test_bootstrap_grid_distinguishes_small_and_large_models():
    X, y = train_model.synthetic_rows_from_rules()
    indexes = {
        name: train_model.FEATURE_ORDER.index(name)
        for name in (
            "ram_gb",
            "vram_gb",
            "unified_memory",
            "param_count_b",
            "quant_bits",
            "context_length",
            "active_param_count_b",
        )
    }

    def speed_for(parameters: float) -> float:
        for features, speed in zip(X, y):
            if (
                features[indexes["ram_gb"]] == 24
                and features[indexes["vram_gb"]] == 24
                and features[indexes["unified_memory"]] == 1
                and features[indexes["param_count_b"]] == parameters
                and features[indexes["quant_bits"]] == 4
                and features[indexes["context_length"]] == 8192
            ):
                return speed
        raise AssertionError("bootstrap point not found")

    assert speed_for(0.5) > speed_for(1.5) > speed_for(7.0)


def test_bootstrap_grid_models_moe_active_parameters_separately():
    X, y = train_model.synthetic_rows_from_rules()
    indexes = {name: train_model.FEATURE_ORDER.index(name) for name in train_model.FEATURE_ORDER}

    def speed_for(total: float, active: float) -> float:
        for features, speed in zip(X, y):
            if (
                features[indexes["ram_gb"]] == 24
                and features[indexes["vram_gb"]] == 24
                and features[indexes["param_count_b"]] == total
                and features[indexes["active_param_count_b"]] == active
                and features[indexes["quant_bits"]] == 4
                and features[indexes["context_length"]] == 4096
            ):
                return speed
        raise AssertionError("bootstrap point not found")

    assert speed_for(30.0, 3.0) > speed_for(27.0, 27.0)


def test_load_telemetry_file_accepts_local_jsonl(tmp_path):
    path = tmp_path / "benchmarks.jsonl"
    path.write_text("\n".join(json.dumps(_row(speed)) for speed in (10, 20)) + "\n")

    rows = train_model.load_telemetry_file(path)

    assert [row["tokens_per_sec"] for row in rows] == [10, 20]


def test_load_telemetry_file_accepts_firebase_mapping(tmp_path):
    path = tmp_path / "firebase.json"
    path.write_text(json.dumps({"push-a": _row(10), "push-b": _row(20)}))

    rows = train_model.load_telemetry_file(path)

    assert len(rows) == 2


def test_load_telemetry_file_accepts_self_hosted_export(tmp_path):
    path = tmp_path / "server-export.json"
    path.write_text(json.dumps({"count": 2, "benchmarks": [_row(10), _row(20)]}))

    rows = train_model.load_telemetry_file(path)

    assert [row["tokens_per_sec"] for row in rows] == [10, 20]


def test_fetch_real_rows_accepts_authenticated_self_hosted_export(monkeypatch):
    captured = {}
    monkeypatch.setenv("LOCALFIT_ADMIN_TOKEN", "secret")

    def fake_get(url, headers, timeout):
        captured.update(url=url, headers=headers, timeout=timeout)
        return _Response({"count": 1, "benchmarks": [_row(22, engine="llama.cpp")]})

    monkeypatch.setattr(train_model.requests, "get", fake_get)

    rows = train_model.fetch_real_rows("https://collector.example/v1/benchmarks/export")

    assert rows[0]["engine"] == "llama.cpp"
    assert captured["headers"] == {"Authorization": "Bearer secret"}


def test_load_telemetry_file_rejects_malformed_jsonl(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text(json.dumps(_row(10)) + "\nnot-json\n")

    with pytest.raises(ValueError, match=":2 is not valid JSON"):
        train_model.load_telemetry_file(path)
