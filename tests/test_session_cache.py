from omm import session_cache


def _fake_tty(monkeypatch, name="/dev/faketty0"):
    monkeypatch.setattr(session_cache.os, "ttyname", lambda fd: name)


def _no_tty(monkeypatch):
    def _raise(fd):
        raise OSError("not a tty")

    monkeypatch.setattr(session_cache.os, "ttyname", _raise)


def test_record_and_load_seen_roundtrips(isolated_omm_home, monkeypatch):
    _fake_tty(monkeypatch)

    session_cache.record_seen(["a", "b"])

    assert session_cache.load_seen() == ["a", "b"]


def test_record_seen_dedupes_and_moves_to_front(isolated_omm_home, monkeypatch):
    _fake_tty(monkeypatch)

    session_cache.record_seen(["a", "b"])
    session_cache.record_seen(["b", "c"])

    assert session_cache.load_seen() == ["b", "c", "a"]


def test_record_seen_caps_at_50(isolated_omm_home, monkeypatch):
    _fake_tty(monkeypatch)

    for i in range(60):
        session_cache.record_seen([f"model-{i}"])

    assert len(session_cache.load_seen()) == 50
    assert "model-59" in session_cache.load_seen()


def test_record_results_overwrites_last_results(isolated_omm_home, monkeypatch):
    _fake_tty(monkeypatch)

    session_cache.record_results(["x", "y"])
    assert session_cache.load_last_results() == ["x", "y"]

    session_cache.record_results(["z"])
    assert session_cache.load_last_results() == ["z"]


def test_record_results_also_updates_seen(isolated_omm_home, monkeypatch):
    _fake_tty(monkeypatch)

    session_cache.record_results(["x", "y"])

    assert session_cache.load_seen() == ["x", "y"]


def test_no_tty_is_a_silent_noop(isolated_omm_home, monkeypatch):
    _no_tty(monkeypatch)

    session_cache.record_seen(["a"])
    session_cache.record_results(["b"])

    assert session_cache.load_seen() == []
    assert session_cache.load_last_results() == []


def test_different_ttys_do_not_share_state(isolated_omm_home, monkeypatch):
    _fake_tty(monkeypatch, "/dev/tty-one")
    session_cache.record_results(["from-tty-one"])

    _fake_tty(monkeypatch, "/dev/tty-two")
    assert session_cache.load_last_results() == []


def test_corrupted_cache_file_is_treated_as_empty(isolated_omm_home, monkeypatch):
    _fake_tty(monkeypatch)
    session_cache.record_seen(["a"])

    from omm import config

    session_dir = config.OMM_HOME / "session"
    for f in session_dir.iterdir():
        f.write_text("{not valid json")

    assert session_cache.load_seen() == []
