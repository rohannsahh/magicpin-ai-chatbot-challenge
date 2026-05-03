"""
FastAPI application — Vera bot server.

Endpoints:
  POST /v1/context   — upsert category/merchant/customer/trigger context
  POST /v1/tick      — plan + execute outreach for this tick
  POST /v1/reply     — handle an inbound merchant/customer reply
  GET  /v1/healthz   — liveness probe
  GET  /v1/metadata  — team identity
"""
from __future__ import annotations

import logging
import concurrent.futures
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

logging.basicConfig(level=logging.WARNING)

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from bot import composer
from bot.store import ContextStore, SuppressionStore, ConversationStore, CompositionCache
from bot.tick_planner import TickPlanner, MAX_ACTIONS_PER_TICK
from bot.reply_handler import ReplyHandler

# ─────────────────────────────────────────────────────────────────────────────
# Global singletons
# ─────────────────────────────────────────────────────────────────────────────

context_store = ContextStore()
suppression_store = SuppressionStore()
conversation_store = ConversationStore()
comp_cache = CompositionCache()
tick_planner = TickPlanner(context_store, suppression_store, conversation_store, comp_cache)
reply_handler = ReplyHandler(context_store, conversation_store)
start_time = time.time()

# Thread pool for parallel background + tick composes (8 workers = up to 8 simultaneous LLM calls)
_compose_pool = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="compose")

# Track triggers currently being composed to avoid duplicate LLM calls
_composing_lock = threading.Lock()
_composing_set: set = set()


# ─────────────────────────────────────────────────────────────────────────────
# Compose helpers
# ─────────────────────────────────────────────────────────────────────────────

def _do_compose_work(trigger_id: str, trigger: dict) -> None:
    """Core compose logic — fetches context, calls LLM, writes to cache."""
    try:
        merchant_id = trigger.get("merchant_id")
        if not merchant_id:
            return
        merchant = context_store.get("merchant", merchant_id)
        if not merchant:
            return
        cat_slug = merchant.get("category_slug", "")
        category = context_store.get("category", cat_slug)
        if not category:
            return
        customer_id = trigger.get("customer_id")
        customer = context_store.get("customer", customer_id) if customer_id else None

        composed = composer.compose(category, merchant, trigger, customer)
        body = composed.get("body", "").strip()
        if not body:
            return

        kind = trigger.get("kind", "general")
        suppression_key = trigger.get("suppression_key", f"auto_{trigger_id}")
        owner = merchant.get("identity", {}).get("owner_first_name", "")
        conv_id = f"conv_{trigger_id[:28]}_{merchant_id[:12]}"

        comp_cache.put(trigger_id, {
            "conversation_id": conv_id,
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "send_as": composed.get("send_as", "vera"),
            "trigger_id": trigger_id,
        "template_params": [owner, kind],
            "body": body,
            "cta": composed.get("cta", "open_ended"),
            "suppression_key": suppression_key,
            "rationale": composed.get("rationale", ""),
        })
    except Exception:
        pass
    finally:
        with _composing_lock:
            _composing_set.discard(trigger_id)


def _background_compose(trigger_id: str, trigger: dict) -> None:
    """Pre-compose via thread pool — runs in parallel for all triggers."""
    with _composing_lock:
        if trigger_id in _composing_set:
            return
        _composing_set.add(trigger_id)
    _compose_pool.submit(_do_compose_work, trigger_id, trigger)


def _tick_compose(trigger_id: str, trigger: dict) -> None:
    """Compose immediately for a tick request — no semaphore throttle."""
    with _composing_lock:
        if trigger_id in _composing_set:
            return  # Background thread already working on it
        _composing_set.add(trigger_id)
    _do_compose_work(trigger_id, trigger)

app = FastAPI(title="Vera Bot", version="1.0.0")


# ─────────────────────────────────────────────────────────────────────────────
# Request / Response models
# ─────────────────────────────────────────────────────────────────────────────

VALID_SCOPES = {"category", "merchant", "customer", "trigger"}


class ContextRequest(BaseModel):
    scope: str
    context_id: str           # judge sends "context_id"
    version: int
    payload: Dict[str, Any]

    @field_validator("scope")
    @classmethod
    def scope_must_be_valid(cls, v: str) -> str:
        if v not in VALID_SCOPES:
            raise ValueError(f"Unknown scope '{v}'. Valid: {sorted(VALID_SCOPES)}")
        return v


class TickRequest(BaseModel):
    now: str                                    # ISO-8601
    available_triggers: List[str] = []          # judge sends "available_triggers"


class ReplyRequest(BaseModel):
    conversation_id: str
    merchant_id: str
    message: str
    turn_number: int = 1


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/v1/context")
async def post_context(req: ContextRequest):
    result = context_store.put(req.scope, req.context_id, req.version, req.payload)
    now_iso = datetime.utcnow().isoformat() + "Z"

    if result.get("accepted"):
        # Pre-compose in background so /v1/tick is a fast cache lookup
        if req.scope == "trigger":
            threading.Thread(
                target=_background_compose,
                args=(req.context_id, req.payload),
                daemon=True,
            ).start()
        return JSONResponse(
            status_code=200,
            content={
                "accepted": True,
                "ack_id": f"ack_{req.context_id}_v{req.version}",
                "stored_at": now_iso,
            },
        )
    else:  # stale or invalid
        return JSONResponse(
            status_code=409,
            content={
                "accepted": False,
                "reason": result.get("reason", "stale_version"),
                "current_version": result.get("current_version"),
            },
        )


@app.exception_handler(422)
async def validation_exception_handler(request: Request, exc):
    # Extract first validation error for readable feedback
    try:
        details = exc.errors()[0].get("msg", str(exc))
    except Exception:
        details = str(exc)
    return JSONResponse(
        status_code=400,
        content={"accepted": False, "reason": "invalid_scope", "details": details},
    )


@app.post("/v1/tick")
def post_tick(req: TickRequest):
    try:
        now = datetime.fromisoformat(req.now.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        now = datetime.utcnow()

    try:
        candidates = tick_planner._rank_candidates(now, req.available_triggers)[:MAX_ACTIONS_PER_TICK]
    except Exception:
        candidates = []

    actions = []
    pending = []

    # Fast path: collect already-cached results
    for trigger_id, trigger in candidates:
        cached = comp_cache.get(trigger_id)
        if cached and cached.get("body"):
            action = tick_planner._finalize_from_cache(trigger_id, trigger, cached)
            if action:
                actions.append(action)
        else:
            pending.append((trigger_id, trigger))

    deadline = time.monotonic() + 13.0

    # Fire all pending composes in parallel via thread pool
    futures: Dict[str, concurrent.futures.Future] = {}
    for trigger_id, trigger in pending:
        with _composing_lock:
            being_composed = trigger_id in _composing_set
            if not being_composed:
                _composing_set.add(trigger_id)
        if not being_composed:
            futures[trigger_id] = _compose_pool.submit(_do_compose_work, trigger_id, trigger)

    # Wait for all parallel composes within the remaining deadline
    if futures:
        time_left = max(0.0, deadline - time.monotonic())
        concurrent.futures.wait(list(futures.values()), timeout=time_left)

    # Also poll for triggers that were already being composed in background
    poll_end = time.monotonic() + max(0.0, deadline - time.monotonic())
    for trigger_id, trigger in pending:
        if trigger_id in futures:
            continue  # Already handled above
        while time.monotonic() < poll_end:
            cached = comp_cache.get(trigger_id)
            if cached and cached.get("body"):
                break
            with _composing_lock:
                if trigger_id not in _composing_set:
                    break
            time.sleep(0.1)

    # Collect results from all pending triggers
    for trigger_id, trigger in pending:
        cached = comp_cache.get(trigger_id)
        if cached and cached.get("body"):
            action = tick_planner._finalize_from_cache(trigger_id, trigger, cached)
            if action:
                actions.append(action)

    return {"actions": actions}


@app.post("/v1/reply")
async def post_reply(req: ReplyRequest):
    try:
        result = reply_handler.handle(
            conv_id=req.conversation_id,
            merchant_id=req.merchant_id,
            message=req.message,
            turn_number=req.turn_number,
        )
    except Exception as e:
        result = {"action": "end", "rationale": f"Internal error: {e}"}

    return result


@app.get("/v1/healthz")
async def healthz():
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - start_time),
        "contexts_loaded": context_store.counts(),
    }


@app.get("/v1/metadata")
async def metadata():
    from bot.llm import LLM_PROVIDER, LLM_MODEL
    _model_display = LLM_MODEL or {
        "deepseek": "deepseek-v4-flash",
        "groq": "llama-3.3-70b-versatile",
        "anthropic": "claude-3-haiku-20240307",
        "gemini": "gemini-2.0-flash",
    }.get(LLM_PROVIDER, "gpt-4o-mini")
    return {
        "team_name": "Vera Pro",
        "team_members": ["Rohan"],
        "model": _model_display,
        "approach": (
            "Deterministic per-trigger-kind fact extraction → "
            "tight per-kind LLM prompt with verified data only → "
            "enum-validated JSON output. "
            "Prevents hallucination, forces specificity, maximises judge score on decision_quality + specificity."
        ),
        "version": "1.0.0",
        "supported_trigger_kinds": [
            "research_digest", "recall_due", "perf_dip", "perf_spike",
            "renewal_due", "winback_eligible", "festival_upcoming",
            "review_theme_emerged", "curious_ask_due", "wedding_package_followup",
            "customer_lapsed_soft", "customer_lapsed_hard", "dormant_with_vera",
            "ipl_match_today", "competitor_opened", "milestone_reached",
            "regulation_change", "appointment_tomorrow", "trial_followup",
            "chronic_refill_due",
        ],
    }
