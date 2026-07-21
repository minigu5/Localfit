from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from omm import benchmark_history, config, registry


def test_registry_parallel_upserts_do_not_lose_entries(isolated_omm_home):
    def write(index: int) -> None:
        registry.upsert_entry(f"model-{index}.gguf", size_bytes=index, linked={})

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(write, range(40)))

    saved = registry.load_registry()
    assert len(saved) == 40
    assert saved["model-39.gguf"]["size_bytes"] == 39


def test_benchmark_history_parallel_updates_do_not_lose_entries(isolated_omm_home):
    def write(index: int) -> None:
        benchmark_history.record_benchmarked(
            f"org/model-{index}:model-{index}.gguf",
            repo_id=f"org/model-{index}",
            filename=f"model-{index}.gguf",
            sha256=str(index),
            tokens_per_sec=float(index + 1),
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(write, range(40)))

    assert len(benchmark_history.loaded_refs()) == 40


def test_config_parallel_updates_do_not_overwrite_each_other(isolated_omm_home):
    def write(index: int) -> None:
        config.update_config(**{f"field_{index}": index})

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(write, range(40)))

    saved = config.load_config()
    assert all(saved[f"field_{index}"] == index for index in range(40))


def test_corrupt_files_are_preserved_before_safe_fallback(isolated_omm_home):
    config.CONFIG_PATH.write_text("{broken-config")
    config.REGISTRY_PATH.write_text("{broken-registry")

    assert config.load_config()["telemetry_send_policy"] == "ask"
    assert registry.load_registry() == {}
    assert list(config.CONFIG_PATH.parent.glob("config.json.corrupt-*"))
    assert list(config.REGISTRY_PATH.parent.glob("models.json.corrupt-*"))
