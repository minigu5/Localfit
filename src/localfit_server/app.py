"""FastAPI application for self-hosted, privacy-minimized benchmark data."""

from __future__ import annotations

import os
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from localfit_server.db import BenchmarkStore


class BenchmarkEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", str_max_length=300)

    ram_gb: float = Field(gt=0, le=4096)
    vram_gb: float | None = Field(default=None, ge=0, le=4096)
    unified_memory: bool
    gpu_tflops: float | None = Field(default=None, ge=0, le=100_000)
    model_installed: str = Field(min_length=1, max_length=300)
    model_repo_id: str | None = Field(default=None, max_length=300)
    model_size_bytes: int | None = Field(default=None, gt=0, le=10**15)
    engine: Literal["llama.cpp", "lmstudio", "ollama", "jan", "gpt4all"]
    benchmark_version: int = Field(ge=1, le=1000)
    recorded_at: datetime
    tokens_per_sec: float = Field(gt=0, le=10_000)
    sample_count: int | None = Field(default=None, ge=1, le=10)
    tokens_per_sec_min: float | None = Field(default=None, gt=0, le=10_000)
    tokens_per_sec_max: float | None = Field(default=None, gt=0, le=10_000)
    runtime_profile: str | None = Field(default=None, max_length=50)
    context_length: int | None = Field(default=None, ge=128, le=10_000_000)
    gpu_offload_percent: int | None = Field(default=None, ge=0, le=100)
    cpu_threads: int | None = Field(default=None, ge=1, le=4096)
    num_batch: int | None = Field(default=None, ge=1, le=1_000_000)
    quality_pack_id: str | None = Field(default=None, max_length=100)
    quality_pack_version: str | None = Field(default=None, max_length=20)
    quality_correct: int | None = Field(default=None, ge=0, le=100)
    quality_total: int | None = Field(default=None, ge=1, le=100)
    quality_accuracy: float | None = Field(default=None, ge=0, le=1)

    @field_validator("model_installed", "model_repo_id")
    @classmethod
    def reject_paths_and_controls(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if any(ord(character) < 32 for character in value) or "\\" in value:
            raise ValueError("control characters and local paths are not allowed")
        if value.startswith("/") or ":/" in value:
            raise ValueError("local paths are not allowed")
        return value

    @model_validator(mode="after")
    def validate_sample_summary(self) -> "BenchmarkEvent":
        bounds = (self.tokens_per_sec_min, self.tokens_per_sec_max)
        if (bounds[0] is None) != (bounds[1] is None):
            raise ValueError("sample minimum and maximum must be supplied together")
        if bounds[0] is not None and not (
            bounds[0] <= self.tokens_per_sec <= bounds[1]
        ):
            raise ValueError("median speed must be inside the sample range")
        if self.sample_count is not None and bounds[0] is None:
            raise ValueError("sample_count requires minimum and maximum")
        return self

    @model_validator(mode="after")
    def validate_quality_summary(self) -> "BenchmarkEvent":
        quality_fields = (
            self.quality_pack_id,
            self.quality_pack_version,
            self.quality_correct,
            self.quality_total,
            self.quality_accuracy,
        )
        if any(f is not None for f in quality_fields) and any(f is None for f in quality_fields):
            raise ValueError("quality fields must all be supplied together")
        if self.quality_correct is not None and self.quality_total is not None:
            if self.quality_correct > self.quality_total:
                raise ValueError("quality_correct cannot exceed quality_total")
        return self


@lru_cache(maxsize=1)
def get_store() -> BenchmarkStore:
    configured = os.getenv("LOCALFIT_DB_PATH", "./localfit.db")
    return BenchmarkStore(Path(configured).expanduser())


def require_admin(authorization: str | None = Header(default=None)) -> None:
    expected = os.getenv("LOCALFIT_ADMIN_TOKEN")
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="LOCALFIT_ADMIN_TOKEN is not configured",
        )
    if authorization != f"Bearer {expected}":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token")


app = FastAPI(
    title="Localfit self-hosted benchmark collector",
    version="0.1.0",
    docs_url="/docs",
    redoc_url=None,
)


@app.get("/healthz")
def health() -> dict[str, str]:
    get_store().count()
    return {"status": "ok", "storage": "sqlite"}


@app.post("/v1/benchmarks", status_code=status.HTTP_201_CREATED)
def create_benchmark(event: BenchmarkEvent) -> dict[str, int | str]:
    result = get_store().insert(event.model_dump(mode="json"))
    return {"id": result.id, "status": "stored" if result.created else "duplicate"}


@app.get("/v1/stats")
def stats() -> dict[str, object]:
    store = get_store()
    return {"count": store.count(), "engines": store.engine_counts()}


@app.get("/v1/benchmarks/export", dependencies=[Depends(require_admin)])
def export_benchmarks(limit: int = Query(default=100_000, ge=1, le=100_000)) -> dict[str, object]:
    rows = get_store().export(limit=limit)
    return {"count": len(rows), "benchmarks": rows}
