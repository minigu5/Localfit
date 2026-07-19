import pytest

from omm import hub
from omm.hub import AmbiguousModelError, resolve_model


class _FakeResponse:
    def __init__(self, siblings):
        self._siblings = siblings

    def raise_for_status(self):
        pass

    def json(self):
        return {"siblings": self._siblings}


def test_resolve_model_raises_ambiguous_error_with_repo_and_candidates(monkeypatch):
    monkeypatch.setattr(
        hub.requests,
        "get",
        lambda url, timeout: _FakeResponse(
            [
                {"rfilename": "llama-2-7b.Q4_K_M.gguf"},
                {"rfilename": "llama-2-7b.Q8_0.gguf"},
                {"rfilename": "README.md"},
            ]
        ),
    )

    with pytest.raises(AmbiguousModelError) as exc_info:
        resolve_model("TheBloke/Llama-2-7B-GGUF")

    err = exc_info.value
    assert err.repo_id == "TheBloke/Llama-2-7B-GGUF"
    assert err.candidates == ["llama-2-7b.Q4_K_M.gguf", "llama-2-7b.Q8_0.gguf"]


def test_rank_quant_variants_orders_fitting_candidates_by_quality_first():
    candidates = [
        "llama-2-7b.Q2_K.gguf",
        "llama-2-7b.Q4_K_M.gguf",
        "llama-2-7b.Q8_0.gguf",
    ]

    ranked = hub.rank_quant_variants(candidates, available_gb=6.0)

    assert [v.filename for v in ranked] == [
        "llama-2-7b.Q4_K_M.gguf",
        "llama-2-7b.Q2_K.gguf",
        "llama-2-7b.Q8_0.gguf",
    ]
    assert ranked[0].fits is True
    assert ranked[1].fits is True
    assert ranked[2].fits is False


def test_rank_quant_variants_marks_unparsable_filename_fit_as_unknown():
    ranked = hub.rank_quant_variants(["mystery-file.gguf"], available_gb=6.0)

    assert ranked[0].fits is None
    assert ranked[0].required_gb is None
