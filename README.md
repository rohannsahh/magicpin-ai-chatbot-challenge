# Vera Pro - magicpin AI Challenge

Vera Pro is a trigger-driven WhatsApp assistant for Indian SMBs. The judge pushes merchant, customer, category, and trigger contexts, and the bot returns high-quality outreach actions scored on specificity, category fit, merchant fit, decision quality, and engagement.

## Quick architecture (at a glance)

```
Judge -> /v1/context -> ContextStore
                     -> background pre-compose (ThreadPool)

Judge -> /v1/tick -> TickPlanner -> CompositionCache hit? -> return action
                                  -> miss -> extract facts -> prompt builder -> LLM -> validate -> cache -> action

Inbound reply -> /v1/reply -> ReplyHandler -> next action/end
```

## Approach in one line

Deterministic fact selection first, LLM wording second.

```
trigger -> extractor(kind) -> verified facts -> kind-specific prompt -> LLM(JSON) -> validated action
```

This keeps outputs grounded and reduces hallucination.

## System architecture

```
bot/
  main.py           FastAPI app + orchestration
  extractors.py     per-trigger deterministic fact extraction
  prompts.py        system + kind instructions + category resolution
  composer.py       extract -> prompt -> llm -> output shaping
  tick_planner.py   candidate ranking, suppression handling, action finalization
  reply_handler.py  inbound conversation handling
  store.py          in-memory context + suppression + conversation + compose cache
  llm.py            provider abstraction (DeepSeek/OpenAI/Groq/Anthropic/Gemini)
```

### Runtime flow

1. `/v1/context` upserts contexts and pre-composes trigger messages in background.
2. `/v1/tick` ranks up to 5 triggers.
3. Cache hits are returned immediately.
4. Cache misses compose in parallel via a thread pool (`max_workers=8`) within the tick deadline.
5. Results are validated and returned as actions.

## API endpoints

- `POST /v1/context` - upsert category, merchant, customer, trigger contexts
- `POST /v1/tick` - return best actions for current tick
- `POST /v1/reply` - handle merchant replies
- `GET /v1/healthz` - liveness + loaded context counts
- `GET /v1/metadata` - team metadata

## Model strategy and evolution

I implemented a multi-provider LLM layer and tested across providers.

- Earlier iteration used Groq heavily for speed/cost.
- Current primary path uses DeepSeek (`deepseek-v4-flash`) because it gave better stability under judge workload.
- OpenAI, Anthropic, and Gemini remain supported fallbacks.

### Provider matrix

| Provider | `LLM_PROVIDER` | Typical model |
|---|---|---|
| DeepSeek | `deepseek` | `deepseek-v4-flash` |
| OpenAI | `openai` | `gpt-4o-mini` |
| Groq | `groq` | `llama-3.1-8b-instant` |
| Anthropic | `anthropic` | `claude-3-haiku-20240307` |
| Gemini | `gemini` | `gemini-2.0-flash` |

DeepSeek calls are configured with low temperature and thinking disabled for latency consistency.

## Key design tradeoffs

1. Parallel compose vs. rate limits
- Parallel compose improves tick latency but increases burst risk on low-RPM providers.
- We mitigate using short timeouts, caching, and deterministic extraction to keep retries low.

2. Deterministic facts vs. generation flexibility
- Strict extraction improves judge specificity/decision quality.
- It can reduce stylistic variety and sometimes hurts engagement if prompt constraints are too rigid.

3. Prompt strictness vs. robustness
- Hard constraints improve category fit in many triggers.
- Over-constrained prompts can occasionally collapse into flat scoring patterns under judge variance.

## Setup and local run

```bash
pip install -r requirements.txt

# .env
LLM_PROVIDER=deepseek
LLM_API_KEY=<your_key>
LLM_MODEL=deepseek-v4-flash
BOT_PORT=8080

python -m uvicorn bot.main:app --host 0.0.0.0 --port 8080
python judge_simulator.py
```

## Deployment

Railway-ready files are included:

- `Procfile`
- `railway.json`

Recommended Railway start command:

```bash
python -m uvicorn bot.main:app --host 0.0.0.0 --port $PORT
```

Set these variables in Railway:

- `LLM_PROVIDER`
- `LLM_API_KEY`
- `LLM_MODEL`
- `BOT_PORT` (optional if using `$PORT`)

## Submission artifact

Generate response pairs with:

```bash
python scripts/build_submission.py
```

This writes `submission.jsonl`.

---
Team: Vera Pro
