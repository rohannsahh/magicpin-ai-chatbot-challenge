"""
In-memory stores for all context, suppression, and conversation state.
Thread-safe with standard threading.Lock.
"""
from __future__ import annotations

import time
from datetime import datetime
from threading import Lock
from typing import Any, Dict, Optional, Tuple


# ─────────────────────────────────────────────────────────────────────────────
# ContextStore
# ─────────────────────────────────────────────────────────────────────────────

class ContextStore:
    """
    Stores all 4 context scopes: category, merchant, customer, trigger.
    Enforces (scope, context_id, version) idempotency rules:
      - same version re-posted → 409 stale_version
      - higher version → replace atomically
      - lower version  → 409 stale_version
    """

    VALID_SCOPES = {"category", "merchant", "customer", "trigger"}

    def __init__(self):
        self._data: Dict[str, Dict[str, Tuple[int, Any]]] = {
            "category": {},
            "merchant": {},
            "customer": {},
            "trigger": {},
        }
        self._lock = Lock()

    def put(self, scope: str, context_id: str, version: int, payload: Any) -> dict:
        if scope not in self.VALID_SCOPES:
            return {
                "accepted": False,
                "reason": "invalid_scope",
                "details": f"scope must be one of {sorted(self.VALID_SCOPES)}",
            }

        with self._lock:
            existing = self._data[scope].get(context_id)
            if existing is not None:
                current_version = existing[0]
                if version <= current_version:
                    return {
                        "accepted": False,
                        "reason": "stale_version",
                        "current_version": current_version,
                    }

            self._data[scope][context_id] = (version, payload)
            return {
                "accepted": True,
                "ack_id": f"ack_{context_id}_v{version}",
                "stored_at": datetime.utcnow().isoformat() + "Z",
            }

    def get(self, scope: str, context_id: str) -> Optional[Any]:
        with self._lock:
            entry = self._data[scope].get(context_id)
            return entry[1] if entry else None

    def get_all(self, scope: str) -> Dict[str, Any]:
        with self._lock:
            return {k: v[1] for k, v in self._data[scope].items()}

    def counts(self) -> Dict[str, int]:
        with self._lock:
            return {scope: len(self._data[scope]) for scope in self.VALID_SCOPES}


# ─────────────────────────────────────────────────────────────────────────────
# SuppressionStore
# ─────────────────────────────────────────────────────────────────────────────

class SuppressionStore:
    """
    Prevents re-sending the same message within a suppression window.
    Keys map to expires_at ISO strings.
    """

    def __init__(self):
        self._data: Dict[str, str] = {}
        self._lock = Lock()

    def is_suppressed(self, key: str, now: datetime) -> bool:
        if not key:
            return False
        with self._lock:
            expires = self._data.get(key)
            if expires is None:
                return False
            try:
                exp_dt = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                exp_naive = exp_dt.replace(tzinfo=None)
                now_naive = now.replace(tzinfo=None) if now.tzinfo else now
                return now_naive < exp_naive
            except Exception:
                return False

    def mark(self, key: str, expires_at: str):
        if not key:
            return
        with self._lock:
            self._data[key] = expires_at

    def clear_expired(self, now: datetime):
        """Housekeeping — remove expired keys."""
        with self._lock:
            to_delete = []
            for k, v in self._data.items():
                try:
                    exp = datetime.fromisoformat(v.replace("Z", "+00:00")).replace(tzinfo=None)
                    now_naive = now.replace(tzinfo=None) if now.tzinfo else now
                    if now_naive >= exp:
                        to_delete.append(k)
                except Exception:
                    pass
            for k in to_delete:
                del self._data[k]


# ─────────────────────────────────────────────────────────────────────────────
# ConversationStore
# ─────────────────────────────────────────────────────────────────────────────

class ConversationStore:
    """
    Tracks in-flight conversation state for multi-turn handling.
    """

    def __init__(self):
        self._data: Dict[str, dict] = {}
        self._lock = Lock()

    def create(
        self,
        conv_id: str,
        merchant_id: str,
        customer_id: Optional[str],
        trigger_id: str,
        first_message: str,
    ):
        with self._lock:
            self._data[conv_id] = {
                "merchant_id": merchant_id,
                "customer_id": customer_id,
                "trigger_id": trigger_id,
                "turns": [{"from": "vera", "body": first_message}],
                "auto_reply_count": 0,
                "intent_state": "qualifying",   # qualifying | action | ended
                "created_at": datetime.utcnow().isoformat(),
            }

    def get(self, conv_id: str) -> Optional[dict]:
        with self._lock:
            return self._data.get(conv_id)

    def add_turn(self, conv_id: str, role: str, body: str):
        with self._lock:
            if conv_id in self._data:
                self._data[conv_id]["turns"].append({"from": role, "body": body})

    def update(self, conv_id: str, updates: dict = None, **kwargs):
        with self._lock:
            if conv_id not in self._data:
                # Create a minimal record so auto-reply counts survive
                self._data[conv_id] = {
                    "merchant_id": "", "customer_id": None, "trigger_id": "",
                    "turns": [], "auto_reply_count": 0,
                    "intent_state": "qualifying",
                    "created_at": datetime.utcnow().isoformat(),
                }
            merged = {}
            if updates:
                merged.update(updates)
            if kwargs:
                merged.update(kwargs)
            self._data[conv_id].update(merged)


# ─────────────────────────────────────────────────────────────────────────────
# CompositionCache
# ─────────────────────────────────────────────────────────────────────────────

class CompositionCache:
    """
    Pre-composed action dicts keyed by trigger_id.
    Populated in background threads when triggers are pushed via /v1/context.
    Makes /v1/tick a fast cache lookup instead of a blocking LLM call.
    """

    def __init__(self):
        self._data: Dict[str, dict] = {}
        self._lock = Lock()

    def put(self, trigger_id: str, action: dict):
        with self._lock:
            self._data[trigger_id] = action

    def get(self, trigger_id: str) -> Optional[dict]:
        with self._lock:
            return self._data.get(trigger_id)

    def clear(self):
        with self._lock:
            self._data.clear()
