from __future__ import annotations

import anthropic
import pytest
from botocore.exceptions import NoCredentialsError
from google.auth.exceptions import DefaultCredentialsError

from app.config import Settings
from app.services import llm


@pytest.fixture
def use_settings(monkeypatch):
    def _apply(**overrides):
        monkeypatch.setattr(llm, "settings", Settings(**overrides))

    return _apply


def test_default_provider_builds_anthropic_client(use_settings):
    use_settings(anthropic_api_key="k")
    assert type(llm.make_client()) is anthropic.AsyncAnthropic


def test_bedrock_provider_builds_mantle_client(use_settings, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    use_settings(llm_provider="bedrock", aws_region="eu-central-1", llm_model="anthropic.claude-sonnet-5")
    client = llm.make_client()
    assert isinstance(client, anthropic.AsyncAnthropicBedrockMantle)
    assert "eu-central-1" in str(client.base_url)


def test_vertex_provider_builds_vertex_client(use_settings):
    use_settings(llm_provider="vertex", vertex_project_id="acme-integrity", vertex_region="europe-west4")
    client = llm.make_client()
    assert isinstance(client, anthropic.AsyncAnthropicVertex)
    assert client.project_id == "acme-integrity"
    assert client.region == "europe-west4"


@pytest.mark.parametrize("exc", [NoCredentialsError(), DefaultCredentialsError("no ADC")])
def test_cloud_credential_errors_read_as_misconfiguration(exc):
    assert "misconfigured" in llm.user_facing_llm_error(exc)


def test_unrelated_errors_stay_generic():
    assert "misconfigured" not in llm.user_facing_llm_error(ValueError("boom"))


async def test_bedrock_without_aws_credentials_reads_as_misconfiguration(use_settings, monkeypatch, tmp_path):
    """Pins the SDK's real failure (a bare RuntimeError) so an SDK upgrade can't silently
    turn it back into the generic 'try again' message."""
    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "none"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "none"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    use_settings(llm_provider="bedrock", aws_region="us-east-1", llm_model="anthropic.claude-sonnet-5")
    with pytest.raises(Exception) as caught:
        async with llm.make_client() as client:
            await client.messages.create(
                model="anthropic.claude-sonnet-5", max_tokens=1, messages=[{"role": "user", "content": "x"}]
            )
    assert "misconfigured" in llm.user_facing_llm_error(caught.value)


def test_response_text_skips_thinking_blocks():
    """Sonnet 5 (Bedrock's Sonnet-class model) returns a thinking block before the answer."""
    message = anthropic.types.Message.model_validate(
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "anthropic.claude-sonnet-5",
            "content": [
                {"type": "thinking", "thinking": "", "signature": "sig"},
                {"type": "text", "text": "What corrosion was found?"},
            ],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 20},
        }
    )
    assert llm.response_text(message) == "What corrosion was found?"
