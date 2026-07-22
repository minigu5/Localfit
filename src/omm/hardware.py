"""Cross-platform hardware scanning for RAM/VRAM/OS detection."""

from __future__ import annotations

import platform
import subprocess
from dataclasses import dataclass

import psutil

RAM_MODEL_CAP_RATIO = 0.80
RAM_SAFETY_RESERVE_RATIO = 0.10
RAM_SAFETY_RESERVE_MIN_GB = 2.0
VRAM_MODEL_CAP_RATIO = 0.90
VRAM_SAFETY_RESERVE_RATIO = 0.05
VRAM_SAFETY_RESERVE_MIN_GB = 0.5


@dataclass
class HardwareInfo:
    os_name: str
    os_version: str
    cpu: str
    ram_total_gb: float
    ram_available_gb: float
    unified_memory: bool
    gpu_name: str | None
    vram_total_gb: float | None
    vram_free_gb: float | None
    gpu_tflops: float | None = None
    cpu_arch: str = "unknown"
    cpu_physical_cores: int = 0
    cpu_logical_cores: int = 0


@dataclass(frozen=True)
class MemoryBudget:
    """Live capacity that can be assigned without crowding other apps."""

    model_budget_gb: float
    ram_budget_gb: float
    vram_budget_gb: float | None
    ram_safety_reserve_gb: float
    vram_safety_reserve_gb: float | None
    constrained_by_live_usage: bool


def calculate_memory_budget(hw: HardwareInfo) -> MemoryBudget:
    """Return a conservative model budget from current free RAM and VRAM.

    ``psutil.available`` includes reclaimable memory. Localfit still leaves a
    proportional reserve for the OS and applications opened after the scan.
    Unified-memory Macs use the RAM result once because CPU and GPU share it.
    """
    ram_reserve = max(
        RAM_SAFETY_RESERVE_MIN_GB,
        hw.ram_total_gb * RAM_SAFETY_RESERVE_RATIO,
    )
    ram_total_cap = hw.ram_total_gb * RAM_MODEL_CAP_RATIO
    ram_live_cap = max(0.0, hw.ram_available_gb - ram_reserve)
    ram_budget = min(ram_total_cap, ram_live_cap)
    ram_constrained = ram_live_cap < ram_total_cap

    if hw.unified_memory or hw.vram_total_gb is None:
        return MemoryBudget(
            model_budget_gb=ram_budget,
            ram_budget_gb=ram_budget,
            vram_budget_gb=None,
            ram_safety_reserve_gb=ram_reserve,
            vram_safety_reserve_gb=None,
            constrained_by_live_usage=ram_constrained,
        )

    vram_total = hw.vram_total_gb
    vram_free = hw.vram_free_gb if hw.vram_free_gb is not None else vram_total
    vram_reserve = max(
        VRAM_SAFETY_RESERVE_MIN_GB,
        vram_total * VRAM_SAFETY_RESERVE_RATIO,
    )
    vram_total_cap = vram_total * VRAM_MODEL_CAP_RATIO
    vram_live_cap = max(0.0, vram_free - vram_reserve)
    vram_budget = min(vram_total_cap, vram_live_cap)
    return MemoryBudget(
        # Backends can split layers between dedicated VRAM and RAM. Using the
        # larger safe pool is conservative and avoids double-counting memory.
        model_budget_gb=max(ram_budget, vram_budget),
        ram_budget_gb=ram_budget,
        vram_budget_gb=vram_budget,
        ram_safety_reserve_gb=ram_reserve,
        vram_safety_reserve_gb=vram_reserve,
        constrained_by_live_usage=(
            ram_constrained or vram_live_cap < vram_total_cap
        ),
    )


_OS_DISPLAY_NAMES = {"Darwin": "macOS"}


def _is_apple_silicon() -> bool:
    return platform.system() == "Darwin" and platform.machine() == "arm64"


def _mac_cpu_brand() -> str:
    try:
        out = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"],
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return platform.processor() or "Unknown"


def _mac_chip_name() -> str:
    try:
        out = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"],
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "Apple Silicon"


def _linux_cpu_model() -> str | None:
    """Return Linux CPU brand; platform.processor() is often only ``x86_64``."""
    try:
        for line in open("/proc/cpuinfo", encoding="utf-8", errors="replace"):
            key, separator, value = line.partition(":")
            if separator and key.strip().lower() in {"model name", "hardware"}:
                model = value.strip()
                if model:
                    return model
    except OSError:
        pass
    return None


def _scan_nvidia_vram() -> tuple[str | None, float | None, float | None]:
    """Return (gpu_name, vram_total_gb, vram_free_gb) or (None, None, None) if unavailable."""
    try:
        import pynvml

        pynvml.nvmlInit()
        try:
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            name = pynvml.nvmlDeviceGetName(handle)
            if isinstance(name, bytes):
                name = name.decode()
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
            total_gb = mem.total / (1024**3)
            free_gb = mem.free / (1024**3)
            return name, total_gb, free_gb
        finally:
            pynvml.nvmlShutdown()
    except Exception:
        return None, None, None


def scan_hardware() -> HardwareInfo:
    vm = psutil.virtual_memory()
    ram_total_gb = vm.total / (1024**3)
    ram_available_gb = vm.available / (1024**3)

    raw_os_name = platform.system()
    os_name = _OS_DISPLAY_NAMES.get(raw_os_name, raw_os_name)
    os_version = platform.release()

    cpu_arch = platform.machine() or "unknown"
    cpu_physical_cores = int(psutil.cpu_count(logical=False) or 0)
    cpu_logical_cores = int(psutil.cpu_count(logical=True) or 0)

    if _is_apple_silicon():
        cpu = _mac_cpu_brand()
        return HardwareInfo(
            os_name=os_name,
            os_version=os_version,
            cpu=cpu,
            ram_total_gb=ram_total_gb,
            ram_available_gb=ram_available_gb,
            unified_memory=True,
            gpu_name=_mac_chip_name(),
            vram_total_gb=ram_total_gb,
            vram_free_gb=ram_available_gb,
            cpu_arch=cpu_arch,
            cpu_physical_cores=cpu_physical_cores,
            cpu_logical_cores=cpu_logical_cores,
        )

    if raw_os_name == "Darwin":
        cpu = _mac_cpu_brand()
    elif raw_os_name == "Linux":
        cpu = _linux_cpu_model() or platform.processor() or cpu_arch
    else:
        cpu = platform.processor() or cpu_arch

    gpu_name, vram_total_gb, vram_free_gb = _scan_nvidia_vram()

    return HardwareInfo(
        os_name=os_name,
        os_version=os_version,
        cpu=cpu,
        ram_total_gb=ram_total_gb,
        ram_available_gb=ram_available_gb,
        unified_memory=False,
        gpu_name=gpu_name,
        vram_total_gb=vram_total_gb,
        vram_free_gb=vram_free_gb,
        cpu_arch=cpu_arch,
        cpu_physical_cores=cpu_physical_cores,
        cpu_logical_cores=cpu_logical_cores,
    )
