"""
Handles merchant replies to ongoing Vera conversations.

Decision tree:
  1. Is it an auto-reply? → wait (first) or end (second)
  2. Is it negative/opt-out? → end
  3. Is it off-topic? → polite redirect send
  4. Is it an action-intent (YES/OK/proceed)? → execute the action immediately
  5. Otherwise → LLM continuation
"""
from __future__ import annotations

import re
from typing import Optional

from bot.store import ContextStore, ConversationStore
from bot import llm


# ─────────────────────────────────────────────────────────────────────────────
# Phrase lists (English + common Hindi transliterations)
# ─────────────────────────────────────────────────────────────────────────────

AUTO_REPLY_PHRASES = [
    "thanks for contacting",
    "thank you for contacting",
    "we will get back",
    "we'll get back",
    "out of office",
    "currently unavailable",
    "auto-reply",
    "auto reply",
    "automatic reply",
    "this is an automated",
    "hum jald hi",
    "jawab denge",
    "contact us again",
    "business hours are",
    "office hours",
]

NEGATIVE_PHRASES = [
    "stop",
    "unsubscribe",
    "opt out",
    "opt-out",
    "band karo",
    "mat bhejo",
    "not interested",
    "no thanks",
    "nahi chahiye",
    "remove me",
    "don't contact",
    "do not contact",
    "block",
    "spam",
    "report",
    "leave me alone",
    "go away",
]

ACTION_INTENT_PHRASES = [
    "yes",
    "ok",
    "okay",
    "haan",
    "ha",
    "sure",
    "proceed",
    "go ahead",
    "confirm",
    "book it",
    "done",
    "agreed",
    "1",
    "2",
    "reply 1",
    "reply 2",
    "chalega",
    "kar do",
    "theek hai",
    "bilkul",
    "perfect",
    "sounds good",
    "let's do it",
]

OFF_TOPIC_KEYWORDS = [
    "loan",
    "insurance",
    "gst",
    "tax refund",
    "government scheme",
    "subsidy",
    "job",
    "vacancy",
    "stock market",
    "crypto",
    "bitcoin",
    "gambling",
    "betting",
    "lottery",
    "investment plan",
]


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _normalize(msg: str) -> str:
    return msg.lower().strip().rstrip("!.?")


def _is_auto_reply(msg: str) -> bool:
    norm = _normalize(msg)
    if any(phrase in norm for phrase in AUTO_REPLY_PHRASES):
        return True
    # Long message with no personal greeting = WhatsApp Business canned response
    if len(msg) > 250 and not re.search(r"\b(i|hum|main|mujhe|hamara)\b", norm):
        return True
    return False


def _is_negative(msg: str) -> bool:
    norm = _normalize(msg)
    return any(phrase in norm for phrase in NEGATIVE_PHRASES)


def _is_action_intent(msg: str) -> bool:
    norm = _normalize(msg)
    # Exact short reply like "yes", "1", "ok"
    if norm in ACTION_INTENT_PHRASES:
        return True
    # Short message (≤25 chars) containing an intent phrase
    if len(norm) <= 25 and any(phrase in norm for phrase in ACTION_INTENT_PHRASES):
        return True
    return False


def _is_off_topic(msg: str) -> bool:
    norm = _normalize(msg)
    return any(kw in norm for kw in OFF_TOPIC_KEYWORDS)


def _build_redirect(merchant_name: str) -> str:
    return (
        f"Thanks for reaching out! I'm Vera, {merchant_name}'s business assistant — "
        f"I can only help with booking, updates, and {merchant_name} services. "
        f"Reply YES to continue or STOP to opt out."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Handler
# ─────────────────────────────────────────────────────────────────────────────

class ReplyHandler:

    def __init__(self, ctx_store: ContextStore, conv_store: ConversationStore):
        self.ctx = ctx_store
        self.conv = conv_store

    def handle(
        self,
        conv_id: str,
        merchant_id: str,
        message: str,
        turn_number: int,
    ) -> dict:
        """
        Process an inbound merchant/customer reply.
        Returns one of:
          {"action": "send", "body": "...", "cta": "...", "rationale": "..."}
          {"action": "wait", "wait_seconds": N, "rationale": "..."}
          {"action": "end", "rationale": "..."}
        """
        conv = self.conv.get(conv_id)
        msg_clean = message.strip()

        # ── 1. Auto-reply detection ────────────────────────────────────────
        if _is_auto_reply(msg_clean):
            # Use turn_number as the signal: if we're past turn 2 it's still
            # auto-replying → give up. Also track count in conv if available.
            auto_count = turn_number - 1  # turn 2 = first reply in conv
            if conv:
                auto_count = conv.get("auto_reply_count", 0) + 1
                self.conv.update(conv_id, auto_reply_count=auto_count)
            if auto_count >= 2:
                return {"action": "end", "rationale": "Two consecutive auto-replies detected — merchant unavailable."}
            return {"action": "wait", "wait_seconds": 3600, "rationale": "Auto-reply detected — waiting 1 hour before next outreach."}

        # ── 2. Negative / opt-out ──────────────────────────────────────────
        if _is_negative(msg_clean):
            if conv:
                self.conv.update(conv_id, {"intent_state": "ended"})
            return {"action": "end", "rationale": "Merchant/customer opted out — conversation closed."}

        # ── 3. Off-topic ───────────────────────────────────────────────────
        if _is_off_topic(msg_clean):
            merchant = self.ctx.get("merchant", merchant_id)
            merchant_name = merchant.get("identity", {}).get("name", "us") if merchant else "us"
            body = _build_redirect(merchant_name)
            return {"action": "send", "body": body, "cta": "binary", "rationale": "Off-topic query — redirected on-mission."}

        # ── 4. Action intent (yes / ok / book) ────────────────────────────
        if _is_action_intent(msg_clean):
            body = self._handle_action(conv_id, conv, merchant_id, msg_clean)
            if conv:
                self.conv.update(conv_id, {"intent_state": "action"})
            return {"action": "send", "body": body, "cta": "none", "rationale": "Action intent confirmed — executing."}

        # ── 5. Continuation (LLM) ──────────────────────────────────────────
        body = self._handle_continuation(conv_id, conv, merchant_id, msg_clean, turn_number)
        if conv:
            self.conv.add_turn(conv_id, "merchant", msg_clean)
        return {"action": "send", "body": body, "cta": "open_ended", "rationale": "Ongoing conversation — LLM continuation."}

    # ── Private ───────────────────────────────────────────────────────────────

    def _handle_action(
        self, conv_id: str, conv: Optional[dict], merchant_id: str, message: str
    ) -> str:
        """LLM executes the promised action — no re-asking."""
        if not conv:
            return "Great! I'll take care of that for you. Let me know if you need anything else."

        initial_body = conv.get("initial_body", "")
        merchant = self.ctx.get("merchant", merchant_id)
        merchant_name = merchant.get("identity", {}).get("name", "") if merchant else ""
        owner = merchant.get("identity", {}).get("owner_first_name", "") if merchant else ""

        system = (
            "You are Vera, a WhatsApp assistant for Indian merchants. "
            "The merchant just said YES to an offer or action. "
            "Acknowledge and EXECUTE the promised next step immediately. "
            "Do not ask any more questions. Keep response under 3 sentences."
        )
        user = (
            f"Merchant: {merchant_name} (owner: {owner})\n"
            f"What Vera originally promised: {initial_body}\n"
            f"Merchant reply: {message}\n"
            f"Compose Vera's confirmation + execution message now."
        )
        try:
            return llm.complete(system, user)
        except Exception:
            return f"Perfect! I'll get that sorted for you, {owner}. Reply STOP at any time to opt out."

    def _handle_continuation(
        self, conv_id: str, conv: Optional[dict], merchant_id: str, message: str, turn: int
    ) -> str:
        """LLM continues the conversation, staying on-mission."""
        merchant = self.ctx.get("merchant", merchant_id)
        merchant_name = merchant.get("identity", {}).get("name", "") if merchant else ""
        owner = merchant.get("identity", {}).get("owner_first_name", "") if merchant else ""

        turns_summary = ""
        if conv:
            recent = conv.get("turns", [])[-4:]
            if recent:
                turns_summary = "\n".join(
                    f"[{t['role']}]: {t['content']}" for t in recent
                )

        system = (
            "You are Vera, a WhatsApp AI assistant for Indian merchants on magicpin. "
            "Stay strictly on-mission: help with booking, business insights, and magicpin features. "
            "Be warm, concise (max 3 sentences), and move toward a specific booking or action. "
            "If the merchant asks a factual question about their business, answer from context. "
            "End with a clear next step or question."
        )
        user = (
            f"Merchant: {merchant_name} (owner: {owner})\n"
            f"Turn: {turn}\n"
            f"Recent conversation:\n{turns_summary}\n"
            f"Merchant just said: {message}\n"
            f"Vera's reply:"
        )
        try:
            return llm.complete(system, user)
        except Exception:
            return f"Thanks for sharing that, {owner}! Shall I help you set something up or would you like a quick insight?"
