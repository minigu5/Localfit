import json

from omm import linker


def test_autoremove_lmstudio_deletes_broken_symlink_and_prunes_empty_dirs(tmp_path, monkeypatch):
    models_dir = tmp_path / "lmstudio" / "models"
    broken_dir = models_dir / "TheBloke" / "TinyLlama-1.1B-Chat-v1.0-GGUF"
    broken_dir.mkdir(parents=True)
    broken_link = broken_dir / "tinyllama.gguf"
    broken_link.symlink_to(tmp_path / "does-not-exist.gguf")

    live_target = tmp_path / "real.gguf"
    live_target.write_bytes(b"data")
    live_dir = models_dir / "org" / "repo"
    live_dir.mkdir(parents=True)
    live_link = live_dir / "real.gguf"
    live_link.symlink_to(live_target)

    monkeypatch.setattr(linker, "lmstudio_models_dir", lambda: models_dir)

    removed = linker.autoremove_lmstudio()

    assert removed == 1
    assert not broken_link.is_symlink()
    assert not broken_dir.exists()  # emptied parent got pruned
    assert live_link.is_symlink()  # untouched


def test_autoremove_lmstudio_returns_zero_when_dir_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(linker, "lmstudio_models_dir", lambda: tmp_path / "missing")

    assert linker.autoremove_lmstudio() == 0


def test_autoremove_ollama_removes_broken_blob_and_its_manifest(tmp_path, monkeypatch):
    models_dir = tmp_path / "ollama"
    blobs_dir = models_dir / "blobs"
    blobs_dir.mkdir(parents=True)

    broken_digest_hex = "a" * 64
    broken_blob = blobs_dir / f"sha256-{broken_digest_hex}"
    broken_blob.symlink_to(tmp_path / "gone.gguf")

    live_digest_hex = "b" * 64
    live_blob = blobs_dir / f"sha256-{live_digest_hex}"
    live_target = tmp_path / "alive.gguf"
    live_target.write_bytes(b"data")
    live_blob.symlink_to(live_target)

    manifests_root = models_dir / "manifests" / "registry.ollama.ai" / "library"

    broken_manifest_dir = manifests_root / "broken-model"
    broken_manifest_dir.mkdir(parents=True)
    (broken_manifest_dir / "latest").write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "config": {"digest": f"sha256:{broken_digest_hex}", "size": 1},
                "layers": [{"digest": f"sha256:{broken_digest_hex}", "size": 1}],
            }
        )
    )

    live_manifest_dir = manifests_root / "alive-model"
    live_manifest_dir.mkdir(parents=True)
    (live_manifest_dir / "latest").write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "config": {"digest": f"sha256:{live_digest_hex}", "size": 1},
                "layers": [{"digest": f"sha256:{live_digest_hex}", "size": 1}],
            }
        )
    )

    monkeypatch.setattr(linker, "ollama_models_dir", lambda: models_dir)

    blobs_removed, manifests_removed = linker.autoremove_ollama()

    assert blobs_removed == 1
    assert manifests_removed == 1
    assert not broken_blob.exists()
    assert not (broken_manifest_dir / "latest").exists()
    assert live_blob.is_symlink()
    assert (live_manifest_dir / "latest").exists()


def test_autoremove_ollama_returns_zero_when_blobs_dir_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(linker, "ollama_models_dir", lambda: tmp_path / "missing")

    assert linker.autoremove_ollama() == (0, 0)
