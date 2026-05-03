# Vera Pro — magicpin AI Challenge Submission

## What this bot does

Vera Pro is a WhatsApp AI assistant for Indian merchants. The judge pushes merchant data + "trigger events" (e.g. "renewal due in 12 days", "calls dropped 50%") to the bot, which must reply with the best possible WhatsApp message for that situation. An LLM judge scores each message 0–50 across: specificity, category fit, merchant fit, decision quality, engagement.

**Current best result: ~39.6 / 50 average** (11 messages scored)

---

## Core design principle

> Message quality is won at the **data-selection layer**, not the generation layer.

Instead of dumping all merchant data at the LLM, we extract only what matters per trigger type first:

```
trigger_kind → extractor(category, merchant, trigger, customer) → facts_dict
facts_dict   → build_user_prompt(facts, kind)                   → prompt
prompt       → LLM (JSON mode, temp=0)                          → {body, cta, send_as, rationale}
```

The LLM does **language assembly only** — all decisions (which numbers to use, which offer to show, which slot to name) are made deterministically before the LLM is called.

---

## Architecture

```
bot/
  main.py           FastAPI server — 5 endpoints + compose orchestration
  extractors.py     20 per-kind deterministic fact extractors
  prompts.py        SYSTEM_PROMPT + per-kind psychological levers + prompt builder
  composer.py       Orchestrates: extract → prompt → LLM → validate
  tick_planner.py   Urgency-ranks triggers, caches composed results
  reply_handler.py  Detects auto-replies / negative intent / action intent
  store.py          In-memory context, suppression, conversation, compose cache
  llm.py            Multi-provider LLM client (OpenAI / Groq / DeepSeek / Anthropic / Gemini)
```

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/context` | Upsert category / merchant / customer / trigger data |
| POST | `/v1/tick` | Return up to 5 outreach actions for this tick |
| POST | `/v1/reply` | Handle inbound merchant reply, return next action |
| GET  | `/v1/healthz` | Liveness probe |
| GET  | `/v1/metadata` | Team identity |

### Compose flow (the critical path)

```
/v1/context (trigger) → background pre-compose (semaphore=1)
                                    ↓
/v1/tick → rank candidates → fast cache lookup → if miss: sequential compose
                                                   (deadline = 13s, one at a time)
```

**Why sequential, not parallel**: Groq/DeepSeek have RPM limits. Spawning 5 parallel LLM calls causes a burst of 429s → 4–30s retry delays → exceed the 13s tick deadline → 0 actions returned. Sequential compose (one at a time) fits 5 × ~2s = 10s comfortably within the deadline.

---

## Score dimensions & how we target each

| Judge dimension | Technique |
|---|---|
| **Specificity** (8–9/10) | Extractors inject every relevant number verbatim; system prompt rule: "include every number from FACTS" |
| **Category fit** (9/10) | Per-category voice profiles + banned words enforced; category-specific offer patterns |
| **Merchant fit** (8–9/10) | Owner name, merchant display name, city, exact metrics from that merchant's data |
| **Decision quality** (7–8/10) | Extractors prevent hallucinated numbers; only active offers shown; per-kind CTA rules |
| **Engagement** (6–7/10) | Psychological levers: loss aversion for perf_dip/renewal, curiosity for research_digest, countdown for festival/wedding |

---

## Trigger kinds handled (20)

`research_digest` · `recall_due` · `perf_dip` · `perf_spike` · `renewal_due` · `winback_eligible` · `festival_upcoming` · `review_theme_emerged` · `curious_ask_due` · `wedding_package_followup` · `customer_lapsed_soft` · `customer_lapsed_hard` · `dormant_with_vera` · `ipl_match_today` · `competitor_opened` · `milestone_reached` · `regulation_change` · `appointment_tomorrow` · `trial_followup` · `chronic_refill_due`

---

## Key bugs fixed during development

| Bug | Impact | Fix |
|---|---|---|
| `{variable}` literal output | 8B LLM printed `{days_remaining}` literally in messages | Rewrote all 13 prompt instructions to use plain English examples |
| Parallel compose → 429 burst | 5 simultaneous LLM calls → rate limit → 0 actions returned | Changed to sequential compose in `/v1/tick` |
| Extractor crashes (4 kinds) | `dormant_with_vera`, `customer_lapsed`, `trial_followup`, `chronic_refill` returned empty facts | Fixed field access bugs in extractors.py |
| Background compose duplication | Same trigger composed twice simultaneously | `_composing_set` prevents duplicate concurrent LLM calls |

---

## Running the bot

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure provider (choose one)
# .env for DeepSeek V4 (recommended — high rate limits, low cost):
LLM_PROVIDER=deepseek
LLM_API_KEY=<your DeepSeek API key from platform.deepseek.com>
LLM_MODEL=deepseek-v4-flash      # or deepseek-v4-pro for best quality
BOT_PORT=8080

# .env for Groq (free tier — 30 RPM limit causes 429s on batch 4+):
LLM_PROVIDER=groq
LLM_API_KEY=<your Groq key>
LLM_MODEL=llama-3.1-8b-instant

# 3. Start the server
python -m uvicorn bot.main:app --host 0.0.0.0 --port 8080

# 4. Run the judge simulator (separate terminal)
python judge_simulator.py
```

## Supported LLM providers

| Provider | `.env` value | Recommended model | Notes |
|---|---|---|---|
| **DeepSeek V4** | `deepseek` | `deepseek-v4-flash` / `deepseek-v4-pro` | Best quality + high RPM limits |
| OpenAI | `openai` | `gpt-4o-mini` | Good quality, cost moderate |
| Groq | `groq` | `llama-3.1-8b-instant` | Free but 30 RPM cap causes 429s |
| Anthropic | `anthropic` | `claude-3-haiku-20240307` | Good quality |
| Gemini | `gemini` | `gemini-2.0-flash` | Good quality |

---

## What would push scores above 42/50

- Stronger engagement copy in `research_digest`, `festival_upcoming`, `review_theme_emerged` (currently 38/50)
- Richer `review_theme_emerged` hooks (real GBP photo metadata)
- Smarter `recall_due` timing (visit-history time-series per customer)
- Concrete slot labels for `recall_due` / `wedding_package_followup` from real availability data

---
*Team: Vera Pro · Supports: DeepSeek V4 / GPT-4o / Groq / Claude · Temperature: 0*
