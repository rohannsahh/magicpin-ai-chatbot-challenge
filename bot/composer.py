"""
Orchestrates: extract facts → build prompt → LLM → validate → return.

compose() is the single public entry point.
Never raises — falls back to a deterministic template message on any error.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from bot import extractors, prompts, llm

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Validator
# ─────────────────────────────────────────────────────────────────────────────

def _strip_taboos(body: str, taboos: list) -> str:
    for taboo in taboos:
        pattern = re.compile(re.escape(taboo), re.IGNORECASE)
        body = pattern.sub("", body)
    return re.sub(r" {2,}", " ", body).strip()


def _validate(result: dict, category: dict) -> dict:
    body = result.get("body", "").strip()
    cta = result.get("cta", "open_ended")
    send_as = result.get("send_as", "vera")
    rationale = result.get("rationale", "")

    # Strip taboo words
    taboos = category.get("voice", {}).get("vocab_taboo", [])
    if taboos:
        body = _strip_taboos(body, taboos)

    # Normalise enums
    if cta not in ("binary", "open_ended", "none"):
        cta = "open_ended"
    if send_as not in ("vera", "merchant_on_behalf"):
        send_as = "vera"

    # Cap at 700 chars — trim at last full sentence
    if len(body) > 700:
        sentences = re.split(r"(?<=[.!?])\s+", body)
        trimmed = ""
        for s in sentences:
            if len(trimmed) + len(s) + 1 <= 680:
                trimmed += (" " if trimmed else "") + s
            else:
                break
        if trimmed:
            body = trimmed

    return {"body": body, "cta": cta, "send_as": send_as, "rationale": rationale}


# ─────────────────────────────────────────────────────────────────────────────
# Fallback (deterministic, no LLM)
# ─────────────────────────────────────────────────────────────────────────────

def _fallback(facts: dict, trigger_kind: str, send_as: str) -> dict:
    owner = facts.get("owner") or facts.get("customer_name", "there")
    trigger_reason = facts.get("trigger_reason", trigger_kind.replace("_", " "))
    active_offers = facts.get("active_offers") or []
    merchant_name = facts.get("merchant_name", "")

    offer_line = f" We have {active_offers[0]} currently available." if active_offers else ""
    cta_line = " Reply YES to explore, or STOP to opt out."

    body = f"Hi {owner}, {trigger_reason}.{offer_line}{cta_line}"
    if merchant_name and send_as == "merchant_on_behalf":
        body = f"Hi {owner}, {merchant_name} here — {trigger_reason}.{offer_line} Reply YES to book or STOP."

    return {
        "body": body,
        "cta": "binary",
        "send_as": send_as,
        "rationale": f"Fallback template: {trigger_kind} — trigger reason + active offer + binary CTA",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def compose(
    category: dict,
    merchant: dict,
    trigger: dict,
    customer: Optional[dict] = None,
) -> dict:
    """
    Main composition function.

    Returns dict with keys: body, cta, send_as, rationale
    Never raises — guaranteed to return something usable.
    """
    trigger_kind = trigger.get("kind", "generic")
    trigger_scope = trigger.get("scope", "merchant")
    default_send_as = "merchant_on_behalf" if trigger_scope == "customer" else "vera"

    # ── Step 1: Extract facts ─────────────────────────────────────────────
    try:
        facts = extractors.extract(trigger_kind, category, merchant, trigger, customer)
    except Exception:
        facts = {
            "owner": merchant.get("identity", {}).get("owner_first_name", ""),
            "trigger_reason": trigger_kind.replace("_", " "),
            "merchant_name": merchant.get("identity", {}).get("name", ""),
            "active_offers": [o["title"] for o in merchant.get("offers", []) if o.get("status") == "active"],
        }

    # ── Step 2: Build prompts ─────────────────────────────────────────────
    try:
        system = prompts.SYSTEM_PROMPT
        user = prompts.build_user_prompt(facts, trigger_kind, category)
    except Exception as e:
        logger.warning("FALLBACK[%s] prompt_build failed: %s", trigger_kind, e)
        return _fallback(facts, trigger_kind, default_send_as)

    # ── Step 3: LLM call ──────────────────────────────────────────────────
    try:
        result = llm.complete_json(system, user)
    except Exception as e:
        logger.warning("FALLBACK[%s] llm_call failed: %s", trigger_kind, e)
        return _fallback(facts, trigger_kind, default_send_as)

    # ── Step 4: Validate ──────────────────────────────────────────────────
    try:
        validated = _validate(result, category)
    except Exception as e:
        logger.warning("FALLBACK[%s] validate failed: %s", trigger_kind, e)
        return _fallback(facts, trigger_kind, default_send_as)

    # Ensure body is not empty after validation
    if not validated.get("body"):
        logger.warning("FALLBACK[%s] empty body after validate", trigger_kind)
        return _fallback(facts, trigger_kind, default_send_as)

    return validated
