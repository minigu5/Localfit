import json

from omm import registry, scan_import


def _write_manifest(manifests_root, namespace, name, tag, digest_hex, size=100):
    manifest_dir = manifests_root / "registry.ollama.ai" / namespace / name
    manifest_dir.mkdir(parents=True)
    (manifest_dir / tag).write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "config": {"digest": f"sha256:{'c' * 64}", "size": 1},
                "layers": [
                    {
                        "mediaType": "application/vnd.ollama.image.model",
                        "digest": f"sha256:{digest_hex}",
                        "size": size,
                    }
                ],
            }
        )
    )


def test_scan_ollama_skips_config_blobs_and_symlinks(tmp_path, monkeypatch):
    models_dir = tmp_path / "ollama"
    blobs_dir = models_dir / "blobs"
    manifests_root = models_dir / "manifests"
    blobs_dir.mkdir(parents=True)

    model_digest = "a" * 64
    (blobs_dir / f"sha256-{model_digest}").write_bytes(b"gguf-bytes")

    # config blob shares the same sha256-<hash> naming but isn't a model layer
    config_digest = "c" * 64
    (blobs_dir / f"sha256-{config_digest}").write_bytes(b"{}")

    # already-symlinked model blob (previously adopted) must be skipped
    symlinked_digest = "b" * 64
    linked_target = tmp_path / "already-in-hub.gguf"
    linked_target.write_bytes(b"x")
    (blobs_dir / f"sha256-{symlinked_digest}").symlink_to(linked_target)

    _write_manifest(manifests_root, "library", "llama3", "latest", model_digest)
    _write_manifest(manifests_root, "library", "already-linked", "latest", symlinked_digest)

    monkeypatch.setattr(scan_import.linker, "ollama_models_dir", lambda: models_dir)

    found = scan_import.scan_ollama()

    assert len(found) == 1
    assert found[0].sha256 == model_digest
    assert found[0].display_name == "llama3:latest"
    assert found[0].engine == "ollama"


def test_scan_lmstudio_skips_symlinks(tmp_path, monkeypatch):
    models_dir = tmp_path / "lmstudio" / "models"
    real_dir = models_dir / "org" / "repo"
    real_dir.mkdir(parents=True)
    real_file = real_dir / "model.gguf"
    real_file.write_bytes(b"gguf-bytes")

    link_dir = models_dir / "org2" / "repo2"
    link_dir.mkdir(parents=True)
    (link_dir / "linked.gguf").symlink_to(tmp_path / "elsewhere.gguf")

    monkeypatch.setattr(scan_import.linker, "lmstudio_models_dir", lambda: models_dir)

    found = scan_import.scan_lmstudio()

    assert len(found) == 1
    assert found[0].path == real_file
    assert found[0].display_name == "model.gguf"


def test_group_by_hash_merges_identical_files_across_engines(tmp_path):
    a = scan_import.ExternalGguf("ollama", "llama3:latest", tmp_path / "a", 10, "same-hash")
    b = scan_import.ExternalGguf("lmstudio", "model.gguf", tmp_path / "b", 10, "same-hash")
    c = scan_import.ExternalGguf("lmstudio", "other.gguf", tmp_path / "c", 5, "different-hash")

    groups = scan_import.group_by_hash([a, b, c])

    by_hash = {g.sha256: g for g in groups}
    assert len(groups) == 2
    assert sorted(loc.engine for loc in by_hash["same-hash"].locations) == ["lmstudio", "ollama"]
    assert by_hash["same-hash"].display_name == "model.gguf"  # prefers the real LM Studio filename
    assert by_hash["different-hash"].engines == ["lmstudio"]


def test_adopt_group_merges_duplicate_across_engines_and_reports_saved_bytes(isolated_omm_home, tmp_path):
    payload = b"identical gguf bytes"

    ollama_path = tmp_path / "ollama-blob"
    ollama_path.write_bytes(payload)
    lmstudio_dir = tmp_path / "lmstudio" / "org" / "repo"
    lmstudio_dir.mkdir(parents=True)
    lmstudio_path = lmstudio_dir / "model.gguf"
    lmstudio_path.write_bytes(payload)

    group = scan_import.ModelGroup(
        sha256="deadbeef",
        locations=[
            scan_import.ExternalGguf("ollama", "llama3:latest", ollama_path, len(payload), "deadbeef"),
            scan_import.ExternalGguf("lmstudio", "model.gguf", lmstudio_path, len(payload), "deadbeef"),
        ],
    )

    result = scan_import.adopt_group(group)

    hub_path = scan_import.MODELS_DIR / "model.gguf"
    assert hub_path.exists() and not hub_path.is_symlink()
    assert hub_path.read_bytes() == payload
    assert ollama_path.is_symlink() and ollama_path.resolve() == hub_path.resolve()
    assert lmstudio_path.is_symlink() and lmstudio_path.resolve() == hub_path.resolve()
    assert result.bytes_saved == len(payload)  # one of the two copies reclaimed

    entry = registry.load_registry()["model.gguf"]
    assert entry["sha256"] == "deadbeef"
    assert entry["linked"] == {"lmstudio": True, "ollama": True}


def test_adopt_group_reuses_existing_hub_copy_for_same_hash(isolated_omm_home, tmp_path):
    payload = b"already installed via omm"
    hub_file = scan_import.MODELS_DIR / "existing.gguf"
    hub_file.write_bytes(payload)
    registry.upsert_entry(
        "existing.gguf",
        sha256="cafef00d",
        version="cafef00",
        source="https://example.com/existing.gguf",
        size_bytes=len(payload),
        installed_at="2026-01-01T00:00:00+00:00",
        ollama_name="existing",
        repo_id="org/repo",
        linked={"lmstudio": True, "ollama": False},
    )

    stray_dir = tmp_path / "ollama-stray"
    stray_dir.mkdir()
    stray_path = stray_dir / "sha256-cafef00d"
    stray_path.write_bytes(payload)

    group = scan_import.ModelGroup(
        sha256="cafef00d",
        locations=[scan_import.ExternalGguf("ollama", "existing:latest", stray_path, len(payload), "cafef00d")],
    )

    result = scan_import.adopt_group(group)

    assert result.filename == "existing.gguf"
    assert result.bytes_saved == len(payload)
    assert stray_path.is_symlink() and stray_path.resolve() == hub_file.resolve()

    entry = registry.load_registry()["existing.gguf"]
    assert entry["linked"] == {"lmstudio": True, "ollama": True}  # merged, lmstudio flag preserved
