"""Find GGUF files sitting in Ollama's / LM Studio's own directories (or an
arbitrary path) that aren't yet managed by omm, group them by sha256 so
identical copies collapse into one, and adopt the survivors into the omm
hub with a symlink left behind at every original location.

No Typer/console/questionary here so the scan/group/adopt logic stays
directly unit-testable - see cli.py for the interactive prompt flow that
drives this.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from omm import linker, registry
from omm.config import MODELS_DIR, ensure_omm_home
from omm.hashutil import sha256_file

_OLLAMA_MODEL_LAYER = "application/vnd.ollama.image.model"


@dataclass
class ExternalGguf:
    engine: str  # "ollama" | "lmstudio" | "import"
    display_name: str
    path: Path
    size_bytes: int
    sha256: str


@dataclass
class ModelGroup:
    sha256: str
    locations: list[ExternalGguf]

    @property
    def size_bytes(self) -> int:
        return self.locations[0].size_bytes

    @property
    def display_name(self) -> str:
        for loc in self.locations:
            if loc.engine == "lmstudio":
                return loc.display_name
        return self.locations[0].display_name

    @property
    def engines(self) -> list[str]:
        return sorted({loc.engine for loc in self.locations})


@dataclass
class AdoptResult:
    filename: str
    bytes_saved: int


def scan_ollama() -> list[ExternalGguf]:
    """Real (non-symlink) Ollama model-layer blobs - config/manifest blobs
    are skipped by only looking at digests actually referenced as a model
    layer, since every Ollama blob shares the same `sha256-<hash>` naming
    regardless of what it contains."""
    blobs_dir = linker.ollama_models_dir() / "blobs"
    manifests_root = linker.ollama_models_dir() / "manifests"
    if not blobs_dir.exists() or not manifests_root.exists():
        return []

    tags_by_digest: dict[str, list[str]] = {}
    for manifest_path in manifests_root.rglob("*"):
        if not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        rel = manifest_path.relative_to(manifests_root)
        tag = f"{rel.parent.name}:{rel.name}"
        for layer in manifest.get("layers", []):
            if layer.get("mediaType") == _OLLAMA_MODEL_LAYER:
                digest = layer["digest"].removeprefix("sha256:")
                tags_by_digest.setdefault(digest, []).append(tag)

    found = []
    for digest, tags in tags_by_digest.items():
        blob = blobs_dir / f"sha256-{digest}"
        if not blob.is_file() or blob.is_symlink():
            continue
        found.append(ExternalGguf("ollama", tags[0], blob, blob.stat().st_size, digest))
    return found


def scan_lmstudio() -> list[ExternalGguf]:
    base = linker.lmstudio_models_dir()
    if not base.exists():
        return []
    found = []
    for path in base.rglob("*.gguf"):
        if not path.is_file() or path.is_symlink():
            continue
        found.append(ExternalGguf("lmstudio", path.name, path, path.stat().st_size, sha256_file(path)))
    return found


def scan_directory(path: Path) -> list[ExternalGguf]:
    found = []
    for gguf_path in path.rglob("*.gguf"):
        if not gguf_path.is_file() or gguf_path.is_symlink():
            continue
        found.append(
            ExternalGguf("import", gguf_path.name, gguf_path, gguf_path.stat().st_size, sha256_file(gguf_path))
        )
    return found


def find_external_models(extra_path: Path | None = None) -> list[ExternalGguf]:
    found = scan_ollama() + scan_lmstudio()
    if extra_path is not None:
        found.extend(scan_directory(extra_path))
    return found


def group_by_hash(found: list[ExternalGguf]) -> list[ModelGroup]:
    by_hash: dict[str, list[ExternalGguf]] = {}
    for item in found:
        by_hash.setdefault(item.sha256, []).append(item)
    return [ModelGroup(h, locs) for h, locs in by_hash.items()]


def adopt_group(group: ModelGroup) -> AdoptResult:
    """Move one physical copy into the omm hub - or reuse an already
    hub-registered copy under this same sha256 - then replace every other
    location for this hash with a symlink to it. Returns bytes reclaimed."""
    ensure_omm_home()
    reg = registry.load_registry()
    existing_filename = next((fn for fn, e in reg.items() if e.get("sha256") == group.sha256), None)

    linked = {"lmstudio": False, "ollama": False}
    bytes_saved = 0

    if existing_filename:
        hub_path = MODELS_DIR / existing_filename
        linked.update(reg[existing_filename].get("linked", {}))
    else:
        preferred = next((loc for loc in group.locations if loc.engine == "lmstudio"), None)
        if preferred is not None:
            filename = preferred.path.name
        else:
            preferred = group.locations[0]
            filename = f"{linker.sanitize_ollama_tag(preferred.display_name)}.gguf"

        hub_path = MODELS_DIR / filename
        if hub_path.exists():
            hub_path = MODELS_DIR / f"{group.sha256[:12]}-{filename}"
        shutil.move(str(preferred.path), str(hub_path))

    for loc in group.locations:
        if loc.path.resolve() == hub_path.resolve():
            continue
        was_real_file = loc.path.is_file() and not loc.path.is_symlink()
        if loc.path.exists() or loc.path.is_symlink():
            loc.path.unlink()
        loc.path.parent.mkdir(parents=True, exist_ok=True)
        loc.path.symlink_to(hub_path)
        if was_real_file:
            bytes_saved += loc.size_bytes
        if loc.engine in linked:
            linked[loc.engine] = True

    filename = hub_path.name
    if existing_filename:
        registry.upsert_entry(existing_filename, linked=linked)
    else:
        registry.upsert_entry(
            filename,
            sha256=group.sha256,
            version=group.sha256[:7],
            source="imported",
            size_bytes=hub_path.stat().st_size,
            installed_at=datetime.now(timezone.utc).isoformat(),
            ollama_name=linker.sanitize_ollama_tag(filename),
            repo_id=None,
            linked=linked,
        )

    return AdoptResult(filename=filename, bytes_saved=bytes_saved)
