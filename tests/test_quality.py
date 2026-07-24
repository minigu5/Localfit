from __future__ import annotations

import json

import pytest
import requests

from omm import benchmark, quality
from omm.hardware import HardwareInfo


def _hardware() -> HardwareInfo:
    return HardwareInfo(
        os_name="macOS",
        os_version="",
        cpu="private CPU name",
        ram_total_gb=24,
        ram_available_gb=18,
        unified_memory=True,
        gpu_name="private GPU name",
        vram_total_gb=24,
        vram_free_gb=18,
    )


def test_bundled_quality_pack_is_versioned_bounded_and_attributed():
    pack, digest = quality.load_pack()

    assert pack["pack_id"] == "localfit-gsm8k-bilingual-smoke"
    assert pack["pack_version"] == "1.1.0"
    assert len(pack["items"]) == 8
    assert {item["language"] for item in pack["items"]} == {"en", "ko"}
    assert pack["sources"][0]["license"] == "MIT"
    assert len(digest) == 64


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ("FINAL: 18", "18"),
        ("work here\nFINAL = 70,000", "70000"),
        ("The result is 3.0", "3"),
        ("no numeric answer", None),
    ],
)
def test_parse_numeric_answer(response, expected):
    assert quality.parse_numeric_answer(response) == expected


def test_quality_pack_rejects_duplicate_ids(tmp_path):
    pack, _digest = quality.load_pack()
    pack["items"][1]["id"] = pack["items"][0]["id"]
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(pack))

    with pytest.raises(quality.QualityEvaluationError, match="unique"):
        quality.load_pack(path)


def test_evaluate_model_stores_parsed_answers_not_raw_text(monkeypatch):
    pack, _digest = quality.load_pack()
    monkeypatch.setattr(
        quality,
        "_model_metadata",
        lambda tag: {
            "tag": tag,
            "digest": "sha256:abc",
            "size_bytes": 123,
            "format": "gguf",
            "family": "test",
            "parameter_size": "1B",
            "quantization_level": "Q4_K_M",
            "license": "apache-2.0",
            "license_link": None,
            "capabilities": ["completion"],
        },
    )
    answers = iter(item["expected"] for item in pack["items"])

    def fake_generate(tag, prompt, generation, num_predict=None):
        answer = next(answers) if num_predict is None else "1"
        return {
            "response": f"private reasoning must not persist\nFINAL: {answer}",
            "eval_count": 10,
            "eval_duration": 100_000_000,
        }

    monkeypatch.setattr(quality, "_generate", fake_generate)
    result = quality.evaluate_model("model:latest", pack, speed_runs=2)

    assert result["quality"]["accuracy"] == 1.0
    assert result["quality"]["raw_responses_stored"] is False
    assert all("response" not in item for item in result["quality"]["items"])
    assert result["speed"]["samples_tokens_per_sec"] == [100.0, 100.0]


def test_collect_evidence_redacts_hardware_names(monkeypatch):
    monkeypatch.setattr(quality, "ollama_version", lambda: "0.30.10")
    monkeypatch.setattr(
        quality,
        "evaluate_model",
        lambda tag, pack, speed_runs=3: {"tag": tag, "quality": {}, "speed": {}},
    )
    unloaded = []
    monkeypatch.setattr(quality, "unload_model", lambda tag: unloaded.append(tag) or True)

    report = quality.collect_evidence(["model:one"], _hardware())

    assert report["environment"]["ram_gb"] == 24
    assert report["environment"]["raw_hardware_names_stored"] is False
    assert "private CPU name" not in json.dumps(report)
    assert "private GPU name" not in json.dumps(report)
    assert unloaded == ["model:one"]
    assert report["models"][0]["measurement_isolation"]["unloaded_after_run"] is True


def test_unload_model_uses_keep_alive_zero_without_deleting(monkeypatch):
    calls = []
    monkeypatch.setattr(
        quality,
        "_request_json",
        lambda method, path, payload=None, timeout=180: calls.append(
            (method, path, payload, timeout)
        )
        or {},
    )

    assert quality.unload_model("model:latest") is True
    assert calls == [
        (
            "POST",
            "/api/generate",
            {"model": "model:latest", "stream": False, "keep_alive": 0},
            30,
        )
    ]


def test_write_evidence_replaces_atomically(tmp_path):
    path = tmp_path / "nested" / "evidence.json"
    quality.write_evidence({"schema_version": 1}, path)

    assert json.loads(path.read_text()) == {"schema_version": 1}
    assert not path.with_suffix(".json.tmp").exists()


def test_runtime_snapshot_prefers_digest_and_reports_actual_offload(monkeypatch):
    digest = "a" * 64
    monkeypatch.setattr(
        quality,
        "_request_json",
        lambda *args, **kwargs: {
            "models": [
                {
                    "name": "model:latest",
                    "digest": "b" * 64,
                    "context_length": 2048,
                    "size": 100,
                    "size_vram": 0,
                },
                {
                    "name": "other:latest",
                    "digest": digest,
                    "context_length": 4096,
                    "size": 100,
                    "size_vram": 75,
                },
            ]
        },
    )

    snapshot = quality.runtime_snapshot(
        "model:latest",
        digest,
        {"num_ctx": 4096, "num_thread": 8, "num_batch": 512},
    )

    assert snapshot == {
        "context_length": 4096,
        "gpu_offload_percent": 75,
        "cpu_threads": 8,
        "num_batch": 512,
        "runtime_profile": "explicit_ollama_options",
    }


def test_model_metadata_matches_bare_tag_against_implicit_latest_suffix(monkeypatch):
    """Ollama's /api/tags always names entries with a suffix ('mmproj:latest'),
    even when the caller passes the bare tag omm hands around internally
    ('mmproj'). A strict-equality lookup used to report a linked, installed
    model as "not installed"."""

    def fake_request(method, path, payload=None, timeout=10):
        if path == "/api/tags":
            return {
                "models": [
                    {"name": "mmproj:latest", "digest": "sha256:" + "a" * 64, "size": 100}
                ]
            }
        assert path == "/api/show"
        return {"details": {}, "model_info": {}, "capabilities": []}

    monkeypatch.setattr(quality, "_request_json", fake_request)

    metadata = quality._model_metadata("mmproj")

    assert metadata["tag"] == "mmproj"
    assert metadata["digest"] == "sha256:" + "a" * 64


def test_model_metadata_rejects_already_linked_clip_mmproj(monkeypatch):
    """A model linked before omm refused clip/mmproj links (or linked
    manually via `ollama create`) must fail fast with a clear reason instead
    of reaching /api/generate, where Ollama's llama-server crashes with
    "unsupported model architecture: 'clip'" and surfaces as an opaque 500."""

    def fake_request(method, path, payload=None, timeout=10):
        assert path == "/api/tags"
        return {
            "models": [
                {
                    "name": "mmproj:latest",
                    "digest": "sha256:" + "a" * 64,
                    "size": 100,
                    "details": {"family": "clip"},
                }
            ]
        }

    monkeypatch.setattr(quality, "_request_json", fake_request)

    with pytest.raises(quality.QualityEvaluationError, match="multimodal projector"):
        quality._model_metadata("mmproj")


def test_multi_sample_benchmark_reuses_identical_options(monkeypatch):
    calls = []
    monkeypatch.setattr(
        benchmark,
        "benchmark_ollama",
        lambda tag, options=None: calls.append((tag, dict(options or {}))) or 10.0,
    )

    result = benchmark.benchmark_ollama_samples(
        "model:latest", runs=3, options={"num_ctx": 4096, "num_thread": 8}
    )

    assert result["count"] == 3
    assert calls == [("model:latest", {"num_ctx": 4096, "num_thread": 8})] * 3


# --- v7 structured failure telemetry ---------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, body=None):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body

    @property
    def text(self):
        return json.dumps(self._body) if self._body is not None else ""


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            {"error": "model requires more system memory (10.0 GiB) than is available (8.0 GiB)"},
            quality.FAILURE_REASON_OUT_OF_MEMORY,
        ),
        ({"error": "CUDA out of memory"}, quality.FAILURE_REASON_OUT_OF_MEMORY),
        ({"error": "failed to load model"}, quality.FAILURE_REASON_MODEL_LOAD_FAILED),
        ({"error": "this model does not support tool calling"}, quality.FAILURE_REASON_UNSUPPORTED_RUNTIME),
        ({"error": "something else entirely"}, quality.FAILURE_REASON_UNKNOWN),
        (None, quality.FAILURE_REASON_UNKNOWN),
    ],
)
def test_classify_error_response_maps_ollama_error_bodies(body, expected):
    assert quality._classify_error_response(_FakeResponse(500, body)) == expected


def test_request_json_classifies_connect_timeout_as_ollama_unavailable(monkeypatch):
    monkeypatch.setattr(
        quality.requests, "request",
        lambda *a, **k: (_ for _ in ()).throw(requests.exceptions.ConnectTimeout("no route")),
    )
    with pytest.raises(quality.QualityEvaluationError) as excinfo:
        quality._request_json("GET", "/api/tags")
    assert excinfo.value.failure_reason == quality.FAILURE_REASON_OLLAMA_UNAVAILABLE


def test_request_json_classifies_read_timeout_as_generation_timeout(monkeypatch):
    monkeypatch.setattr(
        quality.requests, "request",
        lambda *a, **k: (_ for _ in ()).throw(requests.exceptions.ReadTimeout("slow")),
    )
    with pytest.raises(quality.QualityEvaluationError) as excinfo:
        quality._request_json("POST", "/api/generate")
    assert excinfo.value.failure_reason == quality.FAILURE_REASON_GENERATION_TIMEOUT


def test_request_json_classifies_connection_error_as_connection_error(monkeypatch):
    monkeypatch.setattr(
        quality.requests, "request",
        lambda *a, **k: (_ for _ in ()).throw(requests.exceptions.ConnectionError("reset by peer")),
    )
    with pytest.raises(quality.QualityEvaluationError) as excinfo:
        quality._request_json("GET", "/api/ps")
    assert excinfo.value.failure_reason == quality.FAILURE_REASON_CONNECTION_ERROR


def test_request_json_classifies_oom_response_as_out_of_memory(monkeypatch):
    monkeypatch.setattr(
        quality.requests, "request",
        lambda *a, **k: _FakeResponse(500, {"error": "model requires more system memory than is available"}),
    )
    with pytest.raises(quality.QualityEvaluationError) as excinfo:
        quality._request_json("POST", "/api/generate", {})
    assert excinfo.value.failure_reason == quality.FAILURE_REASON_OUT_OF_MEMORY


def test_request_json_defaults_unclassified_http_errors_to_unknown(monkeypatch):
    monkeypatch.setattr(
        quality.requests, "request", lambda *a, **k: _FakeResponse(503, {"error": "temporarily busy"})
    )
    with pytest.raises(quality.QualityEvaluationError) as excinfo:
        quality._request_json("GET", "/api/tags")
    assert excinfo.value.failure_reason == quality.FAILURE_REASON_UNKNOWN


@pytest.mark.parametrize("reason", sorted(quality.MODEL_UNFIT_REASONS))
def test_outcome_for_failure_reason_model_unfit_lane(reason):
    assert quality.outcome_for_failure_reason(reason) == "model_unfit"


@pytest.mark.parametrize("reason", sorted(quality.TRANSIENT_ERROR_REASONS))
def test_outcome_for_failure_reason_transient_lane(reason):
    assert quality.outcome_for_failure_reason(reason) == "transient_error"


def test_quality_evaluation_error_falls_back_to_unknown_for_bad_reason():
    error = quality.QualityEvaluationError("boom", failure_reason="not-a-real-reason")
    assert error.failure_reason == quality.FAILURE_REASON_UNKNOWN


def test_collect_evidence_preserves_sibling_results_after_one_model_fails(monkeypatch):
    """A model that OOMs must not take down models already evaluated - and
    the loop must still evaluate whatever comes after it."""
    monkeypatch.setattr(quality, "ollama_version", lambda: "0.32.1")
    monkeypatch.setattr(
        quality,
        "_model_metadata",
        lambda tag: {
            "tag": tag, "digest": "sha256:" + "a" * 64, "size_bytes": 900_000_000,
            "format": "gguf", "family": "test", "parameter_size": "7B",
            "quantization_level": "Q4_K_M", "license": None, "license_link": None,
            "capabilities": [],
        },
    )

    def fake_evaluate(tag, pack, speed_runs=3, runtime_options=None, model_metadata=None):
        if tag == "big:latest":
            raise quality.QualityEvaluationError(
                "simulated OOM at /some/local/path", failure_reason=quality.FAILURE_REASON_OUT_OF_MEMORY
            )
        return {
            "tag": tag,
            "quality": {"correct": 6, "total": 8, "accuracy": 0.75},
            "speed": {"median_tokens_per_sec": 40.0, "samples_tokens_per_sec": [40.0], "runs": 1},
        }

    monkeypatch.setattr(quality, "evaluate_model", fake_evaluate)
    monkeypatch.setattr(quality, "unload_model", lambda tag: True)

    report = quality.collect_evidence(["small:latest", "big:latest", "third:latest"], _hardware())

    by_tag = {m["tag"]: m for m in report["models"]}
    assert set(by_tag) == {"small:latest", "big:latest", "third:latest"}
    assert by_tag["small:latest"]["outcome"] == "success"
    assert by_tag["small:latest"]["speed"]["median_tokens_per_sec"] == 40.0
    assert by_tag["third:latest"]["outcome"] == "success"
    assert by_tag["big:latest"]["outcome"] == "model_unfit"
    assert by_tag["big:latest"]["failure_reason"] == "out_of_memory"
    assert "tokens_per_sec" not in by_tag["big:latest"]
    assert "speed" not in by_tag["big:latest"]
    assert "sample_count" not in by_tag["big:latest"]
    assert by_tag["big:latest"]["model_metadata"]["parameter_size"] == "7B"
    assert "simulated OOM" not in json.dumps(report)


def test_collect_evidence_classifies_daemon_unreachable_as_transient(monkeypatch):
    monkeypatch.setattr(quality, "ollama_version", lambda: None)

    def raising_metadata(tag):
        raise quality.QualityEvaluationError(
            "connection refused by 10.0.0.5", failure_reason=quality.FAILURE_REASON_OLLAMA_UNAVAILABLE
        )

    def raising_evaluate(tag, pack, speed_runs=3):
        raise quality.QualityEvaluationError(
            "connection refused by 10.0.0.5", failure_reason=quality.FAILURE_REASON_OLLAMA_UNAVAILABLE
        )

    monkeypatch.setattr(quality, "_model_metadata", raising_metadata)
    monkeypatch.setattr(quality, "evaluate_model", raising_evaluate)
    monkeypatch.setattr(quality, "unload_model", lambda tag: False)

    report = quality.collect_evidence(["small:latest"], _hardware())

    entry = report["models"][0]
    assert entry["outcome"] == "transient_error"
    assert entry["failure_reason"] == "ollama_unavailable"
    assert "model_metadata" not in entry
    assert "attempted_runtime" not in entry


def test_model_unfit_reasons_are_narrow_and_explicit():
    """Only reasons Ollama's own response makes explicit belong here. A
    missing file, a corrupted one, or any other undiagnosed load failure is
    not proof the model doesn't fit this hardware."""
    assert quality.MODEL_UNFIT_REASONS == {
        quality.FAILURE_REASON_OUT_OF_MEMORY,
        quality.FAILURE_REASON_UNSUPPORTED_RUNTIME,
    }
    assert quality.FAILURE_REASON_MODEL_LOAD_FAILED in quality.TRANSIENT_ERROR_REASONS


def test_outcome_for_model_load_failed_is_transient_not_unfit():
    assert quality.outcome_for_failure_reason(quality.FAILURE_REASON_MODEL_LOAD_FAILED) == "transient_error"


def test_model_metadata_not_installed_is_classified_as_transient(monkeypatch):
    """A tag that isn't installed yet could simply not be downloaded - it is
    not evidence this hardware can't run the model."""

    def fake_request(method, path, payload=None, timeout=10):
        assert path == "/api/tags"
        return {"models": []}

    monkeypatch.setattr(quality, "_request_json", fake_request)

    with pytest.raises(quality.QualityEvaluationError) as excinfo:
        quality._model_metadata("missing:latest")
    assert excinfo.value.failure_reason == quality.FAILURE_REASON_MODEL_LOAD_FAILED
    assert quality.outcome_for_failure_reason(excinfo.value.failure_reason) == "transient_error"


def test_collect_evidence_classifies_missing_model_file_as_transient_not_unfit(monkeypatch):
    monkeypatch.setattr(quality, "ollama_version", lambda: "0.32.1")

    def raising_metadata(tag):
        raise quality.QualityEvaluationError(
            f"Ollama model '{tag}' is not installed", failure_reason=quality.FAILURE_REASON_MODEL_LOAD_FAILED
        )

    def raising_evaluate(tag, pack, speed_runs=3):
        raise quality.QualityEvaluationError(
            f"Ollama model '{tag}' is not installed", failure_reason=quality.FAILURE_REASON_MODEL_LOAD_FAILED
        )

    monkeypatch.setattr(quality, "_model_metadata", raising_metadata)
    monkeypatch.setattr(quality, "evaluate_model", raising_evaluate)
    monkeypatch.setattr(quality, "unload_model", lambda tag: False)

    report = quality.collect_evidence(["missing:latest"], _hardware())

    entry = report["models"][0]
    assert entry["outcome"] == "transient_error"
    assert entry["failure_reason"] == "model_load_failed"


def test_failure_entry_never_leaks_raw_exception_text_paths_or_ips(monkeypatch):
    monkeypatch.setattr(quality, "ollama_version", lambda: None)
    secret_message = "C:\\Users\\alice\\secret\\path connection refused by 10.0.0.5"

    def raising_metadata(tag):
        raise quality.QualityEvaluationError(secret_message, failure_reason=quality.FAILURE_REASON_CONNECTION_ERROR)

    def raising_evaluate(tag, pack, speed_runs=3):
        raise quality.QualityEvaluationError(secret_message, failure_reason=quality.FAILURE_REASON_CONNECTION_ERROR)

    monkeypatch.setattr(quality, "_model_metadata", raising_metadata)
    monkeypatch.setattr(quality, "evaluate_model", raising_evaluate)
    monkeypatch.setattr(quality, "unload_model", lambda tag: True)

    report = quality.collect_evidence(["small:latest"], _hardware())

    serialized = json.dumps(report)
    assert secret_message not in serialized
    assert "10.0.0.5" not in serialized
    assert "alice" not in serialized
    entry = report["models"][0]
    assert entry["failure_reason"] == "connection_error"
    assert set(entry.keys()) <= {
        "tag", "outcome", "failure_reason", "measurement_isolation", "model_metadata", "attempted_runtime",
    }
