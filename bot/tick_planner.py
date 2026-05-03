"""
Decides which triggers to fire on each tick.

Filters: suppressed, expired, missing store entry.
Ranks:   urgency desc, then expires_at asc.
Caps:    MAX_ACTIONS_PER_TICK actions returned.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import List, Tuple

from bot.store import ContextStore, SuppressionStore, ConversationStore, CompositionCache
from bot import composer

MAX_ACTIONS_PER_TICK = 5


class TickPlanner:

    def __init__(
        self,
        ctx_store: ContextStore,
        supp_store: SuppressionStore,
        conv_store: ConversationStore,
        comp_cache: CompositionCache = None,
    ):
        self.ctx = ctx_store
        self.supp = supp_store
        self.conv = conv_store
        self.cache = comp_cache

    # ── Public ───────────────────────────────────────────────────────────────

    def plan(self, now: datetime, available_trigger_ids: List[str]) -> list:
        actions = []
        candidates = self._rank_candidates(now, available_trigger_ids)[:MAX_ACTIONS_PER_TICK]

        if not candidates:
            return actions

        if not self.cache:
            # No cache — slow path for each candidate
            for trigger_id, trigger in candidates:
                action = self._compose_action(trigger_id, trigger, now)
                if action:
                    actions.append(action)
            return actions

        # ── Fast path: shared deadline across ALL candidates ─────────────────
        # First pass: instant cache check (no wait) — collect whatever is ready
        pending = []
        for trigger_id, trigger in candidates:
            cached = self.cache.get(trigger_id)
            if cached and cached.get("body"):
                action = self._finalize_from_cache(trigger_id, trigger, cached)
                if action:
                    actions.append(action)
            else:
                pending.append((trigger_id, trigger))

        if not pending:
            return actions

        # Second pass: poll ALL pending candidates together against shared 12s deadline
        deadline = time.monotonic() + 12.0
        while pending and time.monotonic() < deadline:
            still_pending = []
            for trigger_id, trigger in pending:
                cached = self.cache.get(trigger_id)
                if cached and cached.get("body"):
                    action = self._finalize_from_cache(trigger_id, trigger, cached)
                    if action:
                        actions.append(action)
                else:
                    still_pending.append((trigger_id, trigger))
            pending = still_pending
            if pending:
                time.sleep(0.3)

        return actions

    def _finalize_from_cache(self, trigger_id: str, trigger: dict, cached: dict):
        """Record suppression/conv and return the cached action."""
        merchant_id = trigger.get("merchant_id")
        customer_id = trigger.get("customer_id")
        if not merchant_id:
            return None
        conv_id = cached.get("conversation_id",
                              f"conv_{trigger_id[:28]}_{merchant_id[:12]}")
        suppression_key = cached.get("suppression_key", f"auto_{trigger_id}")
        body = cached["body"]
        self.conv.create(conv_id, merchant_id, customer_id, trigger_id, body)
        self.supp.mark(
            suppression_key,
            trigger.get("expires_at", "2099-01-01T00:00:00Z"),
        )
        return cached

    # ── Private ──────────────────────────────────────────────────────────────

    def _rank_candidates(
        self, now: datetime, trigger_ids: List[str]
    ) -> List[Tuple[str, dict]]:
        """Return filtered + sorted (trigger_id, trigger) pairs."""
        candidates = []

        for tid in trigger_ids:
            trigger = self.ctx.get("trigger", tid)
            if not trigger:
                continue

            # Skip expired
            expires_at = trigger.get("expires_at", "2099-01-01T00:00:00Z")
            try:
                exp = datetime.fromisoformat(
                    expires_at.replace("Z", "+00:00")
                ).replace(tzinfo=None)
                if now > exp:
                    continue
            except Exception:
                pass

            # Skip suppressed
            sk = trigger.get("suppression_key", "")
            if sk and self.supp.is_suppressed(sk, now):
                continue

            urgency = trigger.get("urgency", 1)
            candidates.append((urgency, expires_at, tid, trigger))

        # Sort: urgency desc, then expires_at asc (most urgent + most urgent deadline first)
        candidates.sort(key=lambda x: (-x[0], x[1]))
        return [(item[2], item[3]) for item in candidates]

    def _compose_action(self, trigger_id: str, trigger: dict, now: datetime):
        """Load contexts, compose, record suppression, return action dict or None."""
        merchant_id = trigger.get("merchant_id")
        customer_id = trigger.get("customer_id")

        if not merchant_id:
            return None

        # ── Fast path: use pre-composed cache ────────────────────────────────
        if self.cache:
            # Only used when called directly (not via plan()'s shared-deadline path)
            cached = self.cache.get(trigger_id)
            if cached and cached.get("body"):
                return self._finalize_from_cache(trigger_id, trigger, cached)
            return None

        # ── Slow path: LLM composition (fallback if cache miss) ───────────────
        merchant = self.ctx.get("merchant", merchant_id)
        if not merchant:
            return None

        cat_slug = merchant.get("category_slug", "")
        category = self.ctx.get("category", cat_slug)
        if not category:
            return None

        customer = self.ctx.get("customer", customer_id) if customer_id else None

        try:
            composed = composer.compose(category, merchant, trigger, customer)
        except Exception:
            return None

        body = composed.get("body", "").strip()
        if not body:
            return None

        conv_id = f"conv_{trigger_id[:28]}_{merchant_id[:12]}"
        kind = trigger.get("kind", "general")
        suppression_key = trigger.get("suppression_key", f"auto_{trigger_id}")
        owner = merchant.get("identity", {}).get("owner_first_name", "")

        self.conv.create(conv_id, merchant_id, customer_id, trigger_id, body)
        self.supp.mark(
            suppression_key,
            trigger.get("expires_at", "2099-01-01T00:00:00Z"),
        )

        return {
            "conversation_id": conv_id,
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "send_as": composed.get("send_as", "vera"),
            "trigger_id": trigger_id,
            "template_name": f"vera_{kind}_v1",
            "template_params": [owner, kind],
            "body": body,
            "cta": composed.get("cta", "open_ended"),
            "suppression_key": suppression_key,
            "rationale": composed.get("rationale", ""),
        }
