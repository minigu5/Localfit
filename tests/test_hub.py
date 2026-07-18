from omm.hub import resolve_model


def test_resolve_model_appends_gguf_suffix_when_missing():
    resolved = resolve_model("bartowski/Meta-Llama-3.1-8B-Instruct-GGUF:Meta-Llama-3.1-8B-Instruct-Q4_K_M")

    assert resolved.filename == "Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf"
    assert resolved.url.endswith("Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf")


def test_resolve_model_leaves_existing_gguf_suffix_untouched():
    resolved = resolve_model("bartowski/Meta-Llama-3.1-8B-Instruct-GGUF:Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf")

    assert resolved.filename == "Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf"
