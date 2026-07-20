import json
import time

from omm import version_check


def test_cached_remote_head_calls_fetch_on_cold_cache(isolated_omm_home):
    calls = []

    def fetch(ref):
        calls.append(ref)
        return "abc123"

    result = version_check.cached_remote_head(fetch, ref="main")

    assert result == "abc123"
    assert calls == ["main"]


def test_cached_remote_head_uses_cache_within_ttl(isolated_omm_home):
    (isolated_omm_home / "update_check.json").write_text(
        json.dumps({"checked_at": time.time(), "remote_head": "cached_sha"})
    )

    def fetch(ref):
        raise AssertionError("fetch should not be called while cache is warm")

    result = version_check.cached_remote_head(fetch, ref="main", ttl_seconds=1800)

    assert result == "cached_sha"


def test_cached_remote_head_refetches_after_ttl_expires(isolated_omm_home):
    (isolated_omm_home / "update_check.json").write_text(
        json.dumps({"checked_at": time.time() - 9999, "remote_head": "old_sha"})
    )

    result = version_check.cached_remote_head(lambda ref: "new_sha", ref="main", ttl_seconds=1800)

    assert result == "new_sha"


def test_cached_remote_head_caches_none_result_without_refetching(isolated_omm_home):
    calls = []

    def fetch(ref):
        calls.append(ref)
        return None

    first = version_check.cached_remote_head(fetch, ref="main", ttl_seconds=1800)
    second = version_check.cached_remote_head(fetch, ref="main", ttl_seconds=1800)

    assert first is None
    assert second is None
    assert calls == ["main"]
