from dotenv import load_dotenv; load_dotenv()
from bot.llm import _gemini_complete, _gemini_text

r1 = _gemini_complete("You output JSON only.", 'Return {"ok": true}')
print("JSON test:", r1[:80])

r2 = _gemini_text("You are helpful.", "Say hello in one word.")
print("Text test:", r2[:80])
