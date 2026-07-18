from omm import search as search_mod


def test_guess_family_tinyllama():
    assert search_mod.guess_family("tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf") == "TinyLlama"


def test_guess_family_llama_not_confused_by_tinyllama_substring():
    assert search_mod.guess_family("Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf") == "Llama"


def test_guess_family_mistral():
    assert search_mod.guess_family("mistral-7b-instruct-v0.2.Q4_K_M.gguf") == "Mistral"


def test_guess_family_other_for_unknown_name():
    assert search_mod.guess_family("some-random-model-name") == "Other"


def test_local_candidate_pool_merges_curated_and_cached_and_dedupes(monkeypatch):
    monkeypatch.setattr(
        search_mod.predictor,
        "load_model",
        lambda url: {
            "candidates": [
                {
                    "name": "tinyllama-1.1b-q4",
                    "repo_id": "TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF",
                    "filename": "tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf",
                    "description": "Curated default",
                },
                {
                    "name": "qwen2.5-7b-instruct-q4",
                    "repo_id": "Qwen/Qwen2.5-7B-Instruct-GGUF",
                    "filename": "qwen2.5-7b-instruct-q4_k_m.gguf",
                    "description": "Solid 7B",
                },
            ]
        },
    )

    pool = search_mod.local_candidate_pool(None)

    repo_ids = [c["repo_id"] for c in pool]
    assert repo_ids.count("TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF") == 1
    assert "Qwen/Qwen2.5-7B-Instruct-GGUF" in repo_ids
    # 3 curated (tinyllama/llama3.1/mistral) + 1 new qwen from the cache = 4
    assert len(pool) == 4


def test_search_huggingface_returns_empty_list_on_request_error(monkeypatch):
    def _raise(*args, **kwargs):
        raise search_mod.requests.RequestException("boom")

    monkeypatch.setattr(search_mod.requests, "get", _raise)

    assert search_mod.search_huggingface("qwen") == []


def test_search_huggingface_filters_out_fake_provenance_repos(monkeypatch):
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return [
                {"id": "Brian6145/Qwen3.6-27B-Claude-Opus-DeepSeek-Distilled-GGUF"},
                {"id": "TheBloke/Mistral-7B-Instruct-v0.2-GGUF"},
            ]

    monkeypatch.setattr(search_mod.requests, "get", lambda *a, **k: _Resp())

    results = search_mod.search_huggingface("mistral")

    repo_ids = [c["repo_id"] for c in results]
    assert "Brian6145/Qwen3.6-27B-Claude-Opus-DeepSeek-Distilled-GGUF" not in repo_ids
    assert "TheBloke/Mistral-7B-Instruct-v0.2-GGUF" in repo_ids


def test_claims_fake_provenance_detects_closed_model_brand_names():
    assert search_mod._claims_fake_provenance(
        "Brian6145/Qwen3.6-27B-Claude-Opus-DeepSeek-Distilled-GGUF"
    )
    assert search_mod._claims_fake_provenance("some-model-gpt-4-distill-GGUF")
    assert not search_mod._claims_fake_provenance("TheBloke/Mistral-7B-Instruct-v0.2-GGUF")


def test_match_candidates_prefers_substring_match():
    pool = [
        {"name": "mistral-7b-instruct-q4", "repo_id": "TheBloke/Mistral-7B-Instruct-v0.2-GGUF"},
        {"name": "llama3.1-8b-instruct-q4", "repo_id": "bartowski/Meta-Llama-3.1-8B-Instruct-GGUF"},
    ]

    result = search_mod.match_candidates(pool, "mistral")

    assert [c["name"] for c in result] == ["mistral-7b-instruct-q4"]


def test_match_candidates_falls_back_to_fuzzy_match_on_typo():
    pool = [{"name": "mistral-7b-instruct-q4", "repo_id": "TheBloke/Mistral-7B-Instruct-v0.2-GGUF"}]

    result = search_mod.match_candidates(pool, "mistrall")

    assert result == pool


def test_suggest_similar_limits_and_orders_by_closeness():
    pool = [
        {"name": "tinyllama-1.1b-q4"},
        {"name": "llama3.1-8b-instruct-q4"},
        {"name": "mistral-7b-instruct-q4"},
    ]

    suggestions = search_mod.suggest_similar("tinylama-1.1b-q4", pool, limit=2)

    assert len(suggestions) <= 2
    assert suggestions[0]["name"] == "tinyllama-1.1b-q4"


def test_group_by_family_buckets_by_parsed_family():
    pool = [
        {"name": "tinyllama-1.1b-q4", "repo_id": "TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF"},
        {"name": "mistral-7b-instruct-q4", "repo_id": "TheBloke/Mistral-7B-Instruct-v0.2-GGUF"},
    ]

    groups = search_mod.group_by_family(pool)

    assert set(groups.keys()) == {"TinyLlama", "Mistral"}
    assert groups["TinyLlama"][0]["name"] == "tinyllama-1.1b-q4"
