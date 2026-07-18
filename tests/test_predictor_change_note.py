from omm import predictor


def test_load_model_with_change_note_flags_new_data_as_changed(monkeypatch, tmp_path):
    monkeypatch.setattr(predictor, "RECOMMEND_MODEL_PATH", tmp_path / "recommend-model.json")
    monkeypatch.setattr(predictor, "fetch_and_cache_model", lambda url: {"candidates": [{"name": "a"}]})

    artifact, changed = predictor.load_model_with_change_note("http://example.com/model.json")

    assert changed is True
    assert artifact == {"candidates": [{"name": "a"}]}


def test_load_model_with_change_note_flags_identical_refetch_as_unchanged(monkeypatch, tmp_path):
    cache_path = tmp_path / "recommend-model.json"
    cache_path.write_text('{"candidates": [{"name": "a"}]}')
    monkeypatch.setattr(predictor, "RECOMMEND_MODEL_PATH", cache_path)
    monkeypatch.setattr(predictor, "fetch_and_cache_model", lambda url: {"candidates": [{"name": "a"}]})

    artifact, changed = predictor.load_model_with_change_note("http://example.com/model.json")

    assert changed is False
    assert artifact == {"candidates": [{"name": "a"}]}


def test_load_model_with_change_note_unchanged_when_fetch_fails_and_falls_back_to_cache(
    monkeypatch, tmp_path
):
    cache_path = tmp_path / "recommend-model.json"
    cache_path.write_text('{"candidates": [{"name": "a"}]}')
    monkeypatch.setattr(predictor, "RECOMMEND_MODEL_PATH", cache_path)

    def _raise(url):
        raise predictor.requests.RequestException("boom")

    monkeypatch.setattr(predictor, "fetch_and_cache_model", _raise)

    artifact, changed = predictor.load_model_with_change_note("http://example.com/model.json")

    assert changed is False
    assert artifact == {"candidates": [{"name": "a"}]}
