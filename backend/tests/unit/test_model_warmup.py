from unittest.mock import MagicMock, patch

from app.main import _warm_models


def test_warm_models_loads_each_model():
    embedder, reranker = MagicMock(), MagicMock()
    with (
        patch("app.services.embedder._get_model", return_value=embedder),
        patch("app.services.retriever._cross_encoder", return_value=reranker),
        patch("app.routers.query._scrub_pii") as scrub,
    ):
        _warm_models()
    embedder.embed.assert_called_once()
    reranker.predict.assert_called_once()
    scrub.assert_called_once()


def test_warm_models_survives_missing_or_broken_models():
    with (
        patch("app.services.embedder._get_model", side_effect=RuntimeError("bad model")),
        patch("app.services.retriever._cross_encoder", side_effect=ImportError),
        patch("app.routers.query._scrub_pii") as scrub,
    ):
        _warm_models()  # must not raise
    scrub.assert_called_once()
