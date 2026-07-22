from io import StringIO

from omm import hardware


def test_linux_scan_uses_cpu_model_and_core_counts_not_architecture(monkeypatch):
    monkeypatch.setattr(hardware.platform, "system", lambda: "Linux")
    monkeypatch.setattr(hardware.platform, "release", lambda: "6.8")
    monkeypatch.setattr(hardware.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(hardware.platform, "processor", lambda: "")
    monkeypatch.setattr(hardware.psutil, "cpu_count", lambda logical: 12 if logical else 6)
    monkeypatch.setattr(hardware, "_scan_nvidia_vram", lambda: (None, None, None))
    monkeypatch.setattr(
        "builtins.open",
        lambda *args, **kwargs: StringIO("model name\t: AMD Ryzen 5 5600X 6-Core Processor\n"),
    )

    info = hardware.scan_hardware()

    assert info.cpu == "AMD Ryzen 5 5600X 6-Core Processor"
    assert info.cpu_arch == "x86_64"
    assert info.cpu_physical_cores == 6
    assert info.cpu_logical_cores == 12
