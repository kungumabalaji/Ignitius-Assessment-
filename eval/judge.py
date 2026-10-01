"""Optional LLM judge. Enabled when JUDGE_BASE_URL, JUDGE_MODEL and JUDGE_API_KEY are set.

Works with any OpenAI-compatible chat endpoint, e.g. Gemini:
  JUDGE_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
"""
from __future__ import annotations

import json
import os
import re

import httpx

KEYS = ("usefulness", "faithfulness", "resource_fit", "conciseness")
ID_RE = re.compile(r"goal-\d{3}")

PROMPT = """You are grading a workplace coaching API. Score the response from 1 (poor) to 5 (excellent) on:
- usefulness: are the steps concrete and relevant to the user's situation?
- faithfulness: does everything said about each resource match its catalog entry below (no invented content)?
- resource_fit: are the chosen resources the right ones for this situation?
- conciseness: is it direct, without filler or repetition?

User message:
{message}

Catalog entries for the resources the response used:
{catalog}

API response:
{response}

Reply with JSON only: {{"usefulness": n, "faithfulness": n, "resource_fit": n, "conciseness": n, "reason": "one sentence"}}"""


class Judge:
    def __init__(self):
        self.base_url = (os.getenv("JUDGE_BASE_URL") or "").rstrip("/")
        self.model = os.getenv("JUDGE_MODEL")
        self.api_key = os.getenv("JUDGE_API_KEY")
        self.enabled = bool(self.base_url and self.model and self.api_key)

    async def score(self, message: str, body, catalog: dict) -> dict:
        used = dict.fromkeys(ID_RE.findall(json.dumps(body)))
        entries = [catalog[i] for i in used if i in catalog]
        prompt = PROMPT.format(message=message, catalog=json.dumps(entries, indent=1),
                               response=json.dumps(body, indent=1))
        try:
            async with httpx.AsyncClient(timeout=60) as c:
                r = await c.post(f"{self.base_url}/chat/completions",
                                 headers={"Authorization": f"Bearer {self.api_key}"},
                                 json={"model": self.model, "temperature": 0,
                                       "messages": [{"role": "user", "content": prompt}]})
                r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"]
            data = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
            return {k: int(data[k]) for k in KEYS} | {"reason": str(data.get("reason", ""))}
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"[:300]}
