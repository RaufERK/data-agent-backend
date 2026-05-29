"""LiteProxy OpenAI-compatible client for GigaChat (text + vision)."""
from __future__ import annotations
import logging
import re
from functools import lru_cache
from openai import OpenAI
from .http_client import get_http_client
from backend.config import get_settings

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _build_liteproxy_client() -> OpenAI:
    s = get_settings()
    api_key = s.liteproxy_api_key or s.cloudru_api_key  # fallback to legacy key
    base_url = s.liteproxy_url or s.cloudru_base_url
    if not api_key:
        raise RuntimeError("LITEPROXY_API_KEY (or CLOUDRU_API_KEY) is not set")
    logger.info("Building LiteProxy client: base_url=%s", base_url)
    return OpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=s.liteproxy_timeout,
        http_client=get_http_client(),
        max_retries=0,
    )


def _resolve_model(db_key: str, env_model: str) -> str:
    """Priority: db override → env var → default."""
    from backend.services import app_db
    db_val = app_db.get_model_setting(db_key)
    if db_val:
        return db_val
    return env_model


def chat_complete(messages: list[dict], model: str | None = None, **kwargs) -> str:
    client = _build_liteproxy_client()
    m = model or get_text_model()
    logger.info("chat_complete model=%s", m)
    # Disable thinking mode for Qwen3 models — they return verbose reasoning
    # instead of the requested JSON when thinking is enabled
    if "qwen3" in m.lower() or "qwen/qwen3" in m.lower():
        kwargs.setdefault("extra_body", {})["enable_thinking"] = False
    resp = client.chat.completions.create(model=m, messages=messages, **kwargs)
    msg = resp.choices[0].message
    content = msg.content or getattr(msg, "reasoning_content", None) or ""
    # Fallback: strip <think>...</think> block if present
    after_think = re.split(r"</think>", content, maxsplit=1)
    return after_think[-1].strip() if len(after_think) > 1 else content


# ── Text API ──

def get_text_client() -> OpenAI:
    return _build_liteproxy_client()


def get_text_model() -> str:
    return _resolve_model("liteproxy_text_model", get_settings().liteproxy_text_model)


# ── Vision API ──

def get_vision_client() -> OpenAI:
    return _build_liteproxy_client()


def get_vision_model() -> str:
    return _resolve_model("liteproxy_model", get_settings().liteproxy_model)


# ── Legacy aliases (used by older callers) ──

def get_cloudru_client() -> OpenAI:
    return _build_liteproxy_client()


def get_gpt2giga_client() -> OpenAI:
    return _build_liteproxy_client()
