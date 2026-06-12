"""Shared model-tier routing: provider availability checks and credential env setup.

Supported tier formats:
- "gemini-3.5-flash" or "gemini/gemini-3.5-flash"  -> ADK native (Gemini API key)
- "anthropic/claude-sonnet-4-6"                    -> LiteLLM direct (Anthropic key)
- "openai/gpt-5.5"                                 -> LiteLLM direct (OpenAI key)
- "xai/grok-4.3"                                   -> LiteLLM direct (xAI key)
- "vertex_ai/claude-sonnet-4-6"                    -> LiteLLM via Vertex AI MaaS
  (partner models billed to the GCP project; needs GOOGLE_CLOUD_PROJECT + ADC,
  not an API key)
"""
import os
import logging
from typing import List, Optional
from founderscrew.config import settings

logger = logging.getLogger("founderscrew.model_routing")


def tier_unavailable_reason(model: str) -> Optional[str]:
    """Returns why a model tier can't run (missing credentials), or None if usable."""
    m = (model or "").lower()
    if m.startswith("vertex_ai/"):
        if not (settings.get("google.project_id") or os.environ.get("GOOGLE_CLOUD_PROJECT")):
            return "GOOGLE_CLOUD_PROJECT is not set (Vertex AI partner models bill to a GCP project and use ADC, not an API key)"
        return None
    if "openai" in m and not (settings.get("coding_tools.openai_api_key") or os.environ.get("OPENAI_API_KEY")):
        return "OPENAI_API_KEY is not set"
    if ("anthropic" in m or "claude" in m) and not (settings.get("coding_tools.anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY")):
        return "ANTHROPIC_API_KEY is not set"
    if ("xai" in m or "grok" in m) and not (settings.get("coding_tools.xai_api_key") or os.environ.get("XAI_API_KEY")):
        return "XAI_API_KEY is not set"
    if "gemini" in m and not (settings.get("google.api_key") or os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")):
        return "GOOGLE_API_KEY is not set"
    return None


def filter_available_tiers(tiers: List[str]) -> List[str]:
    """Drops tiers whose provider credentials are missing, logging each skip."""
    available = []
    for tier in tiers:
        reason = tier_unavailable_reason(tier)
        if reason:
            logger.info(f"Skipping model tier {tier}: {reason}")
        else:
            available.append(tier)
    return available


def apply_provider_env() -> None:
    """Exports configured credentials so ADK/LiteLLM calls can resolve them."""
    g_key = settings.get("google.api_key")
    if g_key:
        os.environ["GOOGLE_API_KEY"] = g_key
        os.environ["GEMINI_API_KEY"] = g_key

    for cfg_key, env_var in (
        ("coding_tools.openai_api_key", "OPENAI_API_KEY"),
        ("coding_tools.anthropic_api_key", "ANTHROPIC_API_KEY"),
        ("coding_tools.xai_api_key", "XAI_API_KEY"),
    ):
        val = settings.get(cfg_key)
        if val:
            os.environ[env_var] = val

    # Vertex AI MaaS routing (LiteLLM reads VERTEXAI_PROJECT / VERTEXAI_LOCATION;
    # auth comes from Application Default Credentials — automatic on Cloud Run,
    # `gcloud auth application-default login` locally)
    project = settings.get("google.project_id") or os.environ.get("GOOGLE_CLOUD_PROJECT")
    if project:
        os.environ.setdefault("GOOGLE_CLOUD_PROJECT", str(project))
        os.environ.setdefault("VERTEXAI_PROJECT", str(project))
    location = settings.get("google.vertex_location") or os.environ.get("VERTEXAI_LOCATION") or "global"
    os.environ.setdefault("VERTEXAI_LOCATION", str(location))
