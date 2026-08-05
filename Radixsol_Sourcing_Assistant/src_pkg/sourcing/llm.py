"""Gemini wrapper with graceful fallback when no key is configured."""
from __future__ import annotations

import logging
from . import config

log = logging.getLogger("sourcing.llm")
_client = None
_init = False


def _setup():
    global _client, _init
    if _init:
        return
    _init = True
    if not config.GEMINI_API_KEY:
        return
    try:
        from google import genai
        _client = genai.Client(api_key=config.GEMINI_API_KEY)
    except Exception as e:  # pragma: no cover
        log.warning("Gemini init failed: %s", e)
        _client = None


def available() -> bool:
    _setup()
    return _client is not None


def generate(prompt: str, temperature: float = 0.5, max_tokens: int = 700) -> str | None:
    _setup()
    if _client is None:
        return None
    try:
        from google.genai import types
        r = _client.models.generate_content(
            model=config.GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=temperature,
                max_output_tokens=max_tokens,
            ),
        )
        return (getattr(r, "text", "") or "").strip() or None
    except Exception as e:  # pragma: no cover
        log.warning("Gemini generate failed: %s", e)
        return None
