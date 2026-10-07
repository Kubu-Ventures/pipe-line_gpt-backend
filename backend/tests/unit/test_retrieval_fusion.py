"""Unit tests for multi-query result fusion and the no-cross-encoder rerank fallback."""

from __future__ import annotations

from app.services import retriever
from app.services.retriever import fuse_results, rerank_chunks


def _c(cid: str, similarity: float) -> dict:
    return {"id": cid, "similarity": similarity, "text_content": cid}


def test_fusion_ranks_by_position_not_raw_similarity():
    # The question ranks "target" 1st; two generic rephrasings score higher raw similarity elsewhere.
    question = [_c("target", 0.64), _c("a", 0.63)]
    variant_1 = [_c("b", 0.80), _c("c", 0.79)]
    variant_2 = [_c("d", 0.81), _c("e", 0.78)]
    fused = fuse_results([question, variant_1, variant_2])
    by_similarity = sorted(fused, key=lambda c: c["similarity"], reverse=True)
    assert [c["id"] for c in by_similarity].index("target") == 4  # cut off by a top-3 similarity sort
    assert "target" in [c["id"] for c in fused[:3]]
    assert len(fused) == 6


def test_fusion_rewards_agreement_across_variants():
    fused = fuse_results([[_c("x", 0.5), _c("shared", 0.4)], [_c("shared", 0.4), _c("y", 0.5)]])
    assert fused[0]["id"] == "shared"


def test_fusion_dedups_and_keeps_first_seen_dict():
    first = _c("x", 0.5)
    fused = fuse_results([[first], [_c("x", 0.9)]])
    assert fused == [first]


def test_rerank_fallback_keeps_fused_order(monkeypatch):
    def missing():
        raise ImportError("sentence-transformers not installed")

    monkeypatch.setattr(retriever, "_cross_encoder", missing)
    chunks = [_c("first", 0.1), _c("second", 0.9), _c("third", 0.5)]
    assert [c["id"] for c in rerank_chunks("q", chunks, top_k=2)] == ["first", "second"]
