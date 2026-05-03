"""
Thin LLM client. Supports OpenAI, Anthropic, Groq, and Gemini.
Configure via environment variables:
  LLM_PROVIDER = openai | anthropic | groq | gemini   (default: openai)
  LLM_API_KEY  = your key
  LLM_MODEL    = optional model override
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from dotenv import load_dotenv

load_dotenv()

LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "openai").lower()
LLM_API_KEY: str = os.getenv("LLM_API_KEY", "")
LLM_MODEL: str = os.getenv("LLM_MODEL", "")
TIMEOUT: int = 12  # Tick window is 15s; 12s leaves 3s buffer for network + serialization

# Retry settings for 429 rate-limit errors
_MAX_RETRIES = 3
_RETRY_DELAYS = [4, 12, 30]   # seconds to wait before each retry


# ─────────────────────────────────────────────────────────────────────────────
# Provider backends
# ─────────────────────────────────────────────────────────────────────────────

def _openai_complete(system: str, user: str) -> str:
    from openai import OpenAI  # lazy import so missing package → clear error

    client = OpenAI(api_key=LLM_API_KEY)
    model = LLM_MODEL or "gpt-4o-mini"

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0,
        response_format={"type": "json_object"},
        timeout=TIMEOUT,
        max_tokens=800,
    )
    return response.choices[0].message.content


def _anthropic_complete(system: str, user: str) -> str:
    import anthropic  # lazy import

    client = anthropic.Anthropic(api_key=LLM_API_KEY)
    model = LLM_MODEL or "claude-3-haiku-20240307"

    response = client.messages.create(
        model=model,
        max_tokens=800,
        system=system,
        messages=[{"role": "user", "content": user}],
        temperature=0,
    )
    return response.content[0].text


def _openai_text(system: str, user: str) -> str:
    """Non-JSON text completion (used by reply handler)."""
    from openai import OpenAI

    client = OpenAI(api_key=LLM_API_KEY)
    model = LLM_MODEL or "gpt-4o-mini"

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.3,
        timeout=TIMEOUT,
        max_tokens=300,
    )
    return response.choices[0].message.content


def _anthropic_text(system: str, user: str) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=LLM_API_KEY)
    model = LLM_MODEL or "claude-3-haiku-20240307"

    response = client.messages.create(
        model=model,
        max_tokens=300,
        system=system,
        messages=[{"role": "user", "content": user}],
        temperature=0.3,
    )
    return response.content[0].text


def _groq_complete(system: str, user: str) -> str:
    """Groq — uses OpenAI-compatible SDK pointed at Groq's base URL."""
    from openai import OpenAI

    client = OpenAI(
        api_key=LLM_API_KEY,
        base_url="https://api.groq.com/openai/v1",
    )
    model = LLM_MODEL or "llama-3.3-70b-versatile"

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0,
        response_format={"type": "json_object"},
        timeout=TIMEOUT,
        max_tokens=800,
    )
    return response.choices[0].message.content


def _groq_text(system: str, user: str) -> str:
    from openai import OpenAI

    client = OpenAI(
        api_key=LLM_API_KEY,
        base_url="https://api.groq.com/openai/v1",
    )
    model = LLM_MODEL or "llama-3.3-70b-versatile"

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.3,
        timeout=TIMEOUT,
        max_tokens=300,
    )
    return response.choices[0].message.content


def _deepseek_complete(system: str, user: str) -> str:
    """DeepSeek V4 — OpenAI-compatible SDK pointed at DeepSeek's base URL."""
    from openai import OpenAI

    client = OpenAI(
        api_key=LLM_API_KEY,
        base_url="https://api.deepseek.com",
    )
    model = LLM_MODEL or "deepseek-v4-flash"

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0,
        response_format={"type": "json_object"},
        timeout=TIMEOUT,
        max_tokens=5000,
        extra_body={"thinking": {"type": "disabled"}},
    )
    return response.choices[0].message.content


def _deepseek_text(system: str, user: str) -> str:
    from openai import OpenAI

    client = OpenAI(
        api_key=LLM_API_KEY,
        base_url="https://api.deepseek.com",
    )
    model = LLM_MODEL or "deepseek-v4-flash"

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.3,
        timeout=TIMEOUT,
        max_tokens=400,
        extra_body={"thinking": {"type": "disabled"}},
    )
    return response.choices[0].message.content


def _gemini_complete(system: str, user: str) -> str:
    """Gemini via google-genai SDK. Returns JSON string."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=LLM_API_KEY)
    model_name = LLM_MODEL or "gemini-2.0-flash"
    response = client.models.generate_content(
        model=model_name,
        contents=user,
        config=types.GenerateContentConfig(
            system_instruction=system,
            temperature=0,
            max_output_tokens=800,
            response_mime_type="application/json",
        ),
    )
    return response.text


def _gemini_text(system: str, user: str) -> str:
    """Gemini text completion (non-JSON, for reply handler)."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=LLM_API_KEY)
    model_name = LLM_MODEL or "gemini-2.0-flash"
    response = client.models.generate_content(
        model=model_name,
        contents=user,
        config=types.GenerateContentConfig(
            system_instruction=system,
            temperature=0.3,
            max_output_tokens=300,
        ),
    )
    return response.text


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def complete(system: str, user: str) -> str:
    """Raw text completion (for reply handler). Retries on 429."""
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            if LLM_PROVIDER == "anthropic":
                return _anthropic_text(system, user)
            if LLM_PROVIDER == "groq":
                return _groq_text(system, user)
            if LLM_PROVIDER == "gemini":
                return _gemini_text(system, user)
            if LLM_PROVIDER == "deepseek":
                return _deepseek_text(system, user)
            return _openai_text(system, user)
        except Exception as e:
            last_exc = e
            if _is_rate_limit(e) and attempt < _MAX_RETRIES - 1:
                time.sleep(_RETRY_DELAYS[attempt])
                continue
            raise
    raise RuntimeError(f"LLM text call failed after retries: {last_exc}") from last_exc


def _is_rate_limit(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "429" in msg or "rate_limit" in msg or "rate limit" in msg or "too many" in msg


def complete_json(system: str, user: str) -> dict:
    """
    JSON completion. Returns parsed dict.
    Tries native JSON mode first; falls back to regex extraction.
    Retries on 429 rate-limit errors and empty responses with backoff.
    Raises RuntimeError on failure.
    """
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            if LLM_PROVIDER == "anthropic":
                raw = _anthropic_complete(system, user)
            elif LLM_PROVIDER == "groq":
                raw = _groq_complete(system, user)
            elif LLM_PROVIDER == "deepseek":
                raw = _deepseek_complete(system, user)
            elif LLM_PROVIDER == "gemini":
                raw = _gemini_complete(system, user)
            else:
                raw = _openai_complete(system, user)

            # If empty response, retry
            if not raw or not raw.strip():
                if attempt < _MAX_RETRIES - 1:
                    time.sleep(0.5)
                    continue
                raise ValueError("LLM returned empty response after retries")

            break  # success
        except Exception as e:
            last_exc = e
            if _is_rate_limit(e) and attempt < _MAX_RETRIES - 1:
                time.sleep(_RETRY_DELAYS[attempt])
                continue
            raise RuntimeError(f"LLM API call failed ({LLM_PROVIDER}): {e}") from e
    else:
        raise RuntimeError(f"LLM API call failed after retries: {last_exc}") from last_exc

    # Parse JSON
    raw = raw.strip()
    if raw.startswith("{"):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass

    # Try regex extraction
    match = re.search(r"\{[\s\S]*\}", raw)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    # Try to repair truncated JSON (extract body/cta/send_as from partial response)
    repaired = _repair_truncated_json(raw)
    if repaired:
        return repaired

    raise ValueError(f"No valid JSON in LLM response: {raw[:300]}")


def _repair_truncated_json(raw: str) -> dict | None:
    """
    Recover a valid response from a truncated JSON string.
    DeepSeek sometimes cuts off after 'body' and 'cta' are complete but before 'send_as'/'rationale'.
    If we can extract at least the 'body', we can reconstruct a usable response.
    """
    # Extract body value (handles escaped quotes and newlines)
    body_match = re.search(r'"body"\s*:\s*"((?:[^"\\]|\\.)*)"', raw, re.DOTALL)
    if not body_match:
        return None
    body = body_match.group(1).replace('\\"', '"').replace('\\n', '\n').replace('\\\\', '\\')

    cta_match = re.search(r'"cta"\s*:\s*"([^"]+)"', raw)
    cta = cta_match.group(1) if cta_match else "open_ended"

    send_as_match = re.search(r'"send_as"\s*:\s*"([^"]+)"', raw)
    send_as = send_as_match.group(1) if send_as_match else "vera"

    return {"body": body, "cta": cta, "send_as": send_as, "rationale": "recovered"}
