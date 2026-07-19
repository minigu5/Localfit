from omm import telemetry


def test_send_event_skips_when_not_opted_in_and_not_forced(monkeypatch):
    monkeypatch.setattr(
        telemetry,
        "load_config",
        lambda: {"telemetry_opt_in": False, "telemetry_endpoint": "https://example.com"},
    )
    called = []
    monkeypatch.setattr(telemetry.requests, "post", lambda *a, **k: called.append((a, k)))

    telemetry.send_event({"x": 1})

    assert called == []


def test_send_event_sends_when_forced_even_if_not_opted_in(monkeypatch):
    monkeypatch.setattr(
        telemetry,
        "load_config",
        lambda: {"telemetry_opt_in": False, "telemetry_endpoint": "https://example.com"},
    )
    called = []
    monkeypatch.setattr(telemetry.requests, "post", lambda *a, **k: called.append((a, k)))

    telemetry.send_event({"x": 1}, force=True)

    assert len(called) == 1


def test_send_event_forced_still_requires_endpoint(monkeypatch):
    monkeypatch.setattr(
        telemetry,
        "load_config",
        lambda: {"telemetry_opt_in": False, "telemetry_endpoint": None},
    )
    called = []
    monkeypatch.setattr(telemetry.requests, "post", lambda *a, **k: called.append((a, k)))

    telemetry.send_event({"x": 1}, force=True)

    assert called == []


def test_send_event_sends_when_opted_in_without_force(monkeypatch):
    monkeypatch.setattr(
        telemetry,
        "load_config",
        lambda: {"telemetry_opt_in": True, "telemetry_endpoint": "https://example.com"},
    )
    called = []
    monkeypatch.setattr(telemetry.requests, "post", lambda *a, **k: called.append((a, k)))

    telemetry.send_event({"x": 1})

    assert len(called) == 1
