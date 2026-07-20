import json

from omm import benchmark_history


def test_loaded_refs_empty_when_no_file(isolated_omm_home):
    assert benchmark_history.loaded_refs() == set()


def test_has_been_benchmarked_false_for_unknown_ref(isolated_omm_home):
    assert benchmark_history.has_been_benchmarked("org/repo:model.gguf") is False


def test_record_benchmarked_then_has_been_benchmarked_true(isolated_omm_home):
    benchmark_history.record_benchmarked(
        "org/repo:model.gguf",
        repo_id="org/repo",
        filename="model.gguf",
        sha256="deadbeef",
        tokens_per_sec=12.5,
    )

    assert benchmark_history.has_been_benchmarked("org/repo:model.gguf") is True
    assert benchmark_history.loaded_refs() == {"org/repo:model.gguf"}


def test_record_benchmarked_stores_metadata_on_disk(isolated_omm_home):
    benchmark_history.record_benchmarked(
        "org/repo:model.gguf",
        repo_id="org/repo",
        filename="model.gguf",
        sha256="deadbeef",
        tokens_per_sec=12.5,
    )

    data = json.loads((isolated_omm_home / "benchmark_history.json").read_text())
    entry = data["entries"]["org/repo:model.gguf"]
    assert entry["repo_id"] == "org/repo"
    assert entry["filename"] == "model.gguf"
    assert entry["sha256"] == "deadbeef"
    assert entry["tokens_per_sec"] == 12.5
    assert "benchmarked_at" in entry


def test_multiple_records_accumulate(isolated_omm_home):
    benchmark_history.record_benchmarked(
        "a:x.gguf", repo_id="a", filename="x.gguf", sha256="1", tokens_per_sec=1.0
    )
    benchmark_history.record_benchmarked(
        "b:y.gguf", repo_id="b", filename="y.gguf", sha256="2", tokens_per_sec=2.0
    )

    assert benchmark_history.loaded_refs() == {"a:x.gguf", "b:y.gguf"}
