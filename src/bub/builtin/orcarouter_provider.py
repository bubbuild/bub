"""OpenAI-compatible OrcaRouter provider for any-llm completions."""

from __future__ import annotations

from any_llm.providers.openai.base import BaseOpenAIProvider

ORCAROUTER_PROVIDER = "orcarouter"
"""Bub-side provider key for OrcaRouter models.

any-llm-sdk has no named OrcaRouter provider, so Bub resolves the ``orcarouter:``
model prefix itself (see ``AgentSettings.split_model_provider``) and builds this
OpenAI-compatible provider for it.
"""


class OrcaRouterProvider(BaseOpenAIProvider):
    """OpenAI-compatible completions provider backed by the OrcaRouter gateway.

    OrcaRouter exposes a unified OpenAI-compatible endpoint (``/v1``) in front of
    models from multiple providers, and additionally runs gateway-level,
    zero-trust security for AI agents on the same endpoint. Model ids are the
    OrcaRouter-side ids (e.g. ``orcarouter/auto``).
    """

    API_BASE = "https://api.orcarouter.ai/v1"
    ENV_API_KEY_NAME = "ORCAROUTER_API_KEY"
    ENV_API_BASE_NAME = "ORCAROUTER_API_BASE"
    PROVIDER_NAME = ORCAROUTER_PROVIDER
    PROVIDER_DOCUMENTATION_URL = "https://www.orcarouter.ai"

    SUPPORTS_COMPLETION_STREAMING = True
    SUPPORTS_COMPLETION = True
    SUPPORTS_COMPLETION_REASONING = True
    SUPPORTS_RESPONSES = False
    SUPPORTS_LIST_MODELS = True
    SUPPORTS_BATCH = False
    SUPPORTS_IMAGE_GENERATION = False
    SUPPORTS_AUDIO_TRANSCRIPTION = False
    SUPPORTS_AUDIO_SPEECH = False
    SUPPORTS_EMBEDDING = True
    SUPPORTS_MODERATION = False
