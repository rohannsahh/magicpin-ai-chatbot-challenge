# Vera Bot — Build Plan

> **Goal**: Win the magicpin AI Challenge by building a Vera-style WhatsApp bot that scores 47+/50 on the AI-judged rubric.
> **Constraint**: ~4 working hours.
> **Approach**: Deterministic context extraction + trigger-routed LLM composition + version-aware adaptation + auto-reply-aware reply handling.

---

## 1. The Challenge in One Page

### 1.1 What we are building
An HTTP server that the magicpin judge calls. The judge pushes 4 kinds of context (category, merchant, trigger, customer), then ticks every 5 simulated minutes asking us to send proactive WhatsApp messages, then plays the merchant/customer back to us in replies.

### 1.2 What the judge scores (50 points)
| Dimension | Max | What it measures |
|---|---:|---|
| Specificity | 10 | Real numbers/dates/headlines from the contexts, not generic copy |
| Category fit | 10 | Voice, vocabulary, taboos respected per vertical |
| Merchant fit | 10 | Personalised to *this* merchant (name, locality, perf, offers, signals) |
| Trigger relevance | 10 | First sentence names *why now* |
| Engagement compulsion | 10 | One binary CTA + a compulsion lever (curiosity / loss aversion / reciprocity) |

Bonuses & penalties: Phase 3 adaptation (+5/dim), Phase 4 replay (+30 for top 10), operational (-20 max).

### 1.3 The 5 endpoints we must expose
- `POST /v1/context` — store context, version-checked, idempotent by `(scope, context_id, version)`
- `POST /v1/tick` — return zero or more proactive `actions[]`
- `POST /v1/reply` — respond synchronously (`send` / `wait` / `end`) within 30s
- `GET /v1/healthz` — liveness + counts of contexts loaded
- `GET /v1/metadata` — team identity

---

## 2. The Core Insight (this is the whole thesis)

The 10 case studies that score 47-50/50 share one pattern: every winning message is **a tight handful of verifiable facts pulled directly from the contexts**, wrapped in 2-4 sentences of category-appropriate voice, ending in one binary CTA.

The losing pattern is the obvious one: stuff all 4 contexts into one giant prompt and ask the LLM to write something good. The LLM produces fluent generic copy ("Boost your business with Vera!"), hallucinates offers that don't exist, and ignores the specific trigger reason.

### Therefore the architecture is:
1. **Deterministic extractor** pulls only the relevant fields per `trigger.kind` into a small `facts` dict.
2. **Trigger-routed prompt template** wraps those facts with category voice rules and a strict format spec.
3. **LLM composer** at temperature=0 generates 2-4 sentences. It cannot hallucinate because it only sees the facts dict.
4. **Suppression store + version-aware re-composition** captures the Phase 3 adaptation bonus most teams miss.
5. **Auto-reply detector + intent-transition handler** captures the Phase 4 replay bonus most teams miss.

This stack is what separates a 35/50 from a 47+/50.

---

## 3. System Architecture

```
                            ┌───────────────────────────────┐
   POST /v1/context  ─────► │  ContextStore (in-memory)     │
                            │  {scope: {id: (version,data)}}│
                            └───────────────┬───────────────┘
                                            │
                                            ▼
   POST /v1/tick     ────────────► ┌─────────────────────┐
                                   │   TickPlanner       │
                                   │ - filter triggers   │
                                   │ - dedupe by         │
                                   │   suppression_key   │
                                   │ - rank by urgency   │
                                   └──────────┬──────────┘
                                              │ (trigger, merchant, category, customer?)
                                              ▼
                                   ┌─────────────────────┐
                                   │  ContextExtractor   │  per trigger.kind →
                                   │  (deterministic)    │  facts: {owner, perf%, slots, ...}
                                   └──────────┬──────────┘
                                              ▼
                                   ┌─────────────────────┐
                                   │  Composer (LLM)     │  temp=0
                                   │  prompt = template  │  one template per kind family
                                   │  + voice + facts    │
                                   └──────────┬──────────┘
                                              ▼
                                   ┌─────────────────────┐
                                   │  Validator + Guard  │  strip taboos, enforce CTA
                                   │  + record           │  store suppression_key
                                   │  ConversationState  │
                                   └──────────┬──────────┘
                                              ▼
                                   actions[] returned to judge

   POST /v1/reply    ────────────► ┌─────────────────────┐
                                   │  ReplyHandler       │
                                   │ 1. auto-reply?      │ → wait then end
                                   │ 2. negative?        │ → end
                                   │ 3. intent-positive? │ → action mode (no requalify)
                                   │ 4. question/other?  │ → continue mode
                                   └─────────────────────┘
```

### 3.1 Why each layer exists

- **ContextStore**: idempotency + version replacement is in the contract; failing this disqualifies you. Plain dict with a version check.
- **TickPlanner**: prevents resending the same suppression_key, prevents picking a trigger whose `expires_at` has passed, picks highest-urgency first when multiple are active.
- **ContextExtractor**: forces specificity. The LLM cannot invent because it does not see the full payloads.
- **Composer**: LLM only does language assembly, not data selection. Trivially auditable.
- **Validator**: cheap regex pass to remove vocab_taboo words, verify exactly one CTA, cap message length.
- **ReplyHandler**: auto-reply is the highest-frequency real-world failure mode (40-70% per the brief) and is also Phase 4 scenario #1.

---

## 4. Data Model (in-memory)

```python
ContextStore = {
    "category": { "dentists": (version, payload), ... },
    "merchant": { "m_001_drmeera_...": (version, payload), ... },
    "customer": { "c_001_priya_...": (version, payload), ... },
    "trigger":  { "trg_001_...": (version, payload), ... },
}

SuppressionStore = {
    "research:dentists:2026-W17": expires_at_iso,
    ...
}

ConversationState = {
    "conv_001": {
        "merchant_id": "...",
        "customer_id": None,
        "trigger_id": "...",
        "turns": [ {"from": "vera", "body": "..."}, ... ],
        "auto_reply_count": 0,
        "intent_state": "qualifying" | "action" | "ended",
    }
}
```

All in process memory. Rebuild on restart is fine — the judge re-pushes everything in Phase 1.

---

## 5. The Composer Prompt (the critical artifact)

One system prompt template, parameterised per trigger.kind family. Skeleton:

```
You are Vera, magicpin's WhatsApp assistant.

CHANNEL: WhatsApp message to {recipient_role}.
CATEGORY: {category.slug}

VOICE RULES (must follow exactly):
- Tone: {category.voice.tone}
- Register: {category.voice.register}
- Code-mix: {category.voice.code_mix}
- Vocabulary you MAY use: {category.voice.vocab_allowed}
- BANNED words (must not appear): {category.voice.vocab_taboo}

FACTS YOU MAY USE (do not invent any other fact, name, number, or source):
{facts_as_bulleted_yaml}

TRIGGER REASON: {trigger.kind} — {one_line_why_now}

OUTPUT REQUIREMENTS:
- 2 to 4 sentences total
- First sentence MUST reference the trigger reason
- Use the owner first name "{owner}" exactly once
- One binary CTA at the end (YES/STOP style) UNLESS this is a pure-info trigger
- If a digest item is present, end with "— {source}"
- Do NOT use emoji unless the category allows it
- Do NOT mention any offer that is not in the active offers list

Return JSON only:
{
  "body": "<the message>",
  "cta": "binary" | "open_ended" | "none",
  "rationale": "<one short sentence on why this works>"
}
```

### 5.1 Per-kind specialisations
We override only the `OUTPUT REQUIREMENTS` block per kind:

- `research_digest` — must include the source citation; CTA is "want me to draft a patient/customer-ed WhatsApp?"
- `recall_due` — must include customer name + 2 specific slot labels + active offer price; binary slot CTA ("Reply 1 for X, 2 for Y")
- `perf_dip` — must include the metric, the delta_pct, and the peer median; CTA proposes one concrete action
- `perf_spike` — celebrate + propose how to compound the spike; no loss framing
- `renewal_due` — days remaining + plan + renewal amount; binary YES to renew
- `festival_upcoming` — actionable ops advice (stock/staff/menu), not "happy festival"; no CTA if days_until > 60
- `winback_eligible` — days since expiry + lapsed customers added since; binary YES to reactivate
- `review_theme_emerged` — cite the common quote verbatim; propose one operational fix; binary YES
- `curious_ask_due` — open-ended question + immediate reciprocity offer ("I'll turn your answer into a Google post")
- `ipl_match_today` / weather / news — situational counter-intuitive advice; existing-offer leverage

Anything not covered → generic template that still uses extracted facts.

---

## 6. Trigger-Kind Coverage Priority

The dataset has ~15 distinct kinds across 100 triggers. Cover them in this order; each tier raises your floor.

| Tier | Kinds | Why this tier |
|---|---|---|
| **Tier 1** (must) | `research_digest`, `recall_due`, `perf_dip`, `renewal_due`, `winback_eligible` | These dominate the test_pairs.json; ~70% of scoring surface |
| **Tier 2** (should) | `perf_spike`, `festival_upcoming`, `review_theme_emerged`, `curious_ask_due`, `wedding_package_followup` | Raises specificity scores on the long tail |
| **Tier 3** (nice) | `ipl_match_today`, `regulation_change`, `competitor_opened`, `dormant_with_vera`, `appointment_tomorrow` | Edge cases; generic template handles them adequately |

---

## 7. Reply Handler Logic

Replies are scored in the Phase 4 replay test (top 10 only, +30 max). Three scenarios are pre-announced; we handle them explicitly.

```
def handle_reply(state, msg):
    msg_l = normalise(msg)

    # 1. Auto-reply detection (Scenario: Auto-reply hell)
    if matches_auto_reply(msg_l):
        state.auto_reply_count += 1
        if state.auto_reply_count == 1:
            return {"action": "wait", "wait_seconds": 3600,
                    "rationale": "auto-reply detected; back off 1h"}
        return {"action": "end",
                "rationale": "second auto-reply; this is a canned channel; exit"}

    # 2. Hard negative (Scenario: hostile)
    if matches_negative(msg_l):
        return {"action": "end", "rationale": "merchant declined; graceful exit"}

    # 3. Off-topic / unrelated ask (Scenario: hostile #2)
    if is_off_topic(msg_l):
        return {"action": "send",
                "body": polite_redirect(state),
                "cta": "binary",
                "rationale": "stay on mission, polite redirect"}

    # 4. Intent transition (Scenario: intent transition)
    if matches_action_intent(msg_l):
        state.intent_state = "action"
        return llm_action_followup(state, msg)

    # 5. Default — context-aware continuation
    return llm_continuation(state, msg)
```

Auto-reply phrase bank covers English + Hindi canned replies and the common WhatsApp Business defaults.

---

## 8. Phase-by-Phase Strategy (how each phase is won)

### Phase 1 — Warmup (255 contexts pushed)
- Strict version handling on `/v1/context` (return 409 on stale/equal version).
- `/healthz` returns accurate counts. **If counts are wrong, you are disqualified.**

### Phase 2 — Test window (60 sim minutes, ticks every 5)
- For each tick: filter `available_triggers`, drop suppressed/expired, rank by `urgency desc, then expires_at asc`, take top N.
- Compose at most ~3-5 actions per tick (under the 20 cap, well within the 30s budget).
- Always recompute from current store state — never cache.

### Phase 3 — Adaptive injection (the silent +25 max)
- A new `version` of an already-stored context arrives mid-test. Our store replaces atomically.
- Because we never cache composed messages, the next compose() automatically uses the new digest item / new perf number / new customer.
- This single design choice is worth more than any prompt tuning.

### Phase 4 — Replay (top 10 only, +30 max)
- Auto-reply hell: handler exits after 2nd canned message.
- Intent transition: keyword + LLM detection switches `intent_state` to `action`; next message executes the action, never re-qualifies.
- Hostile/off-topic: short, polite, on-mission redirect; no defensiveness.

### Phase 5 — Penalties to avoid
- Timeouts → keep model fast (gpt-4o-mini / claude-haiku / gemini-flash); cap concurrent compose with `asyncio.gather`.
- Healthz failures → no blocking work in /healthz handler.
- Malformed responses → wrap compose in try/except, fall back to a deterministic template message, never 500.

---

## 9. Repository Layout

```
magicpin-ai-challenge/
├── bot/
│   ├── __init__.py
│   ├── main.py                # FastAPI app, 5 endpoints
│   ├── store.py               # ContextStore, SuppressionStore, ConversationState
│   ├── tick_planner.py        # trigger filtering + ranking
│   ├── extractors.py          # per-kind ContextExtractor
│   ├── composer.py            # LLM call + validator
│   ├── prompts.py             # system + per-kind prompt templates
│   ├── reply_handler.py       # auto-reply + intent + redirect logic
│   └── llm.py                 # thin LLM client (one provider)
├── scripts/
│   └── build_submission.py    # generates submission.jsonl from test_pairs.json
├── expanded/                  # generated dataset (already done)
├── submission.jsonl           # 30 lines, final
├── README.md                  # 1 page approach summary
├── requirements.txt
└── plan.md                    # this file
```

---

## 10. Step-by-Step Build Order (4 hours)

### Hour 1 — Skeleton (60 min)
1. `requirements.txt`: `fastapi`, `uvicorn[standard]`, `pydantic`, `httpx`, one LLM SDK (`openai` recommended for portability).
2. `store.py`: `ContextStore` with `put(scope, id, version, payload) -> Accepted | StaleVersion` and `get(scope, id)`. `SuppressionStore` with `is_suppressed(key, now)` and `mark(key, expires_at)`.
3. `main.py`: 5 endpoints. `/v1/context` validates `scope` enum and version rule. `/v1/tick` returns `[]` for now. `/v1/reply` returns `{"action": "end"}`. `/healthz` returns real counts. `/metadata` returns static dict.
4. **Test gate**: `uvicorn bot.main:app --port 8080`, then `python judge_simulator.py`. Must pass warmup. Must not 500 on tick or reply.

### Hour 2 — Composer (75 min)
1. `extractors.py`: dict of `kind -> callable(category, merchant, trigger, customer) -> facts`. Implement Tier 1 kinds (5 functions, ~10 lines each).
2. `prompts.py`: one base system prompt, one user-prompt builder that takes `(facts, kind, send_as)` and produces the message.
3. `llm.py`: `complete(system, user) -> str` with temperature=0, JSON mode if available, 25s timeout.
4. `composer.py`: `compose(category, merchant, trigger, customer) -> ComposedMessage`. Returns dict matching the action shape.
5. `tick_planner.py`: filter `available_triggers` against suppression + expires + presence in store; rank by urgency; cap at 5 actions/tick; call composer; mark suppression.
6. **Test gate**: Run simulator, read the per-message rationale. Identify the worst dimension across messages and fix.

### Hour 3 — Polish + Reply (60 min)
1. `reply_handler.py`: auto-reply phrase bank, negative phrase bank, action-intent phrase bank. `handle_reply()` per §7.
2. Tier 2 kind extractors + prompt overrides (~30 min).
3. Validator pass: regex-strip vocab_taboo words; verify exactly one CTA token; cap at 600 chars.
4. **Test gate**: run simulator twice, target stable 42+/50 average.

### Hour 4 — Package + Deploy (45 min)
1. `scripts/build_submission.py`: load `expanded/test_pairs.json`, for each pair load category/merchant/trigger/customer JSONs, call `compose()` directly (no HTTP), write line to `submission.jsonl`.
2. `README.md`: 1 page — approach, tradeoffs, what additional context would have helped.
3. Deploy: `uvicorn bot.main:app --host 0.0.0.0 --port 8080` then `ngrok http 8080`. Capture public URL.
4. Final simulator run pointed at public URL. Screenshot scores.
5. Submit URL via the magicpin portal.

---

## 11. Risk Register & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| LLM hallucinates offers/sources | High | -3 to -5 per message on Specificity & Merchant Fit | Extractor passes only facts; prompt forbids invention; validator rejects messages mentioning unknown ₹ amounts |
| Tick budget exceeded (30s) | Medium | Empty tick = 0 score that tick | Cap 5 actions/tick; `asyncio.gather` parallel compose; fast model |
| Version-conflict bug | Medium | Disqualification | Unit test the store rules before plugging anything else in |
| Auto-reply not detected | High in Phase 4 | -10 to -20 | Phrase bank + length heuristic + 1-strike wait, 2-strike end |
| Vocab taboo leaks through | Medium | -2 on Category Fit | Post-LLM regex strip; one regenerate attempt if found |
| LLM JSON parse fails | Low-Med | Malformed response = penalty | Try/except → deterministic template fallback; never raise |
| ngrok URL changes mid-test | Low | DQ | Use a paid tunnel or a small Render free instance as backup |

---

## 12. Definition of Done

- [ ] All 5 endpoints respond per the spec (verified with `judge_simulator.py`).
- [ ] Store enforces `(scope, id, version)` idempotency and replacement.
- [ ] At least Tier 1 + Tier 2 trigger kinds have dedicated extractors and prompts.
- [ ] Reply handler covers auto-reply, negative, action-intent, and off-topic paths.
- [ ] Suppression store prevents re-sends within `expires_at`.
- [ ] No composed message references an offer/source/number not present in extracted facts.
- [ ] `submission.jsonl` has exactly 30 lines, one per test pair.
- [ ] `README.md` ≤ 1 page.
- [ ] Public URL is reachable from the open internet, returns 200 on `/healthz`.
- [ ] Local simulator average ≥ 42/50 on the 30 test pairs.

---

## 13. What Wins (the one-paragraph summary)

We win because most teams will dump everything into a prompt and hope. We win by **separating fact selection (deterministic) from language assembly (LLM)**, which eliminates hallucination and forces specificity — the two highest-weighted scoring dimensions. We then capture the two bonus pools most teams ignore: Phase 3 adaptation (won by never caching and trusting the store) and Phase 4 replay (won by an explicit auto-reply + intent-transition handler). Total expected score: 47-50 on the base rubric, +15-20 from Phase 3, +20-25 from Phase 4 if we make top 10. That's the plan.
