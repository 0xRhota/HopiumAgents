"""LLM clients for Account C, all routed through OpenRouter.

Each client is a `Callable[[dict], List[LLMTradeIdea]]`:
  - Input: briefing dict (built by AccountCLLMScout._build_briefing)
  - Output: 0-3 trade ideas with conviction scores

The system prompt forces structured JSON output. We parse defensively —
LLM output JSON quality varies by model. A bad/empty response → [].
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import List, Optional

import requests

from paper_sim.strategies.account_c_llm_scout import LLMTradeIdea

logger = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Model IDs as recognized by OpenRouter (verified via openrouter.ai/models)
MODEL_OPUS = "anthropic/claude-opus-4-7"           # may fall back to claude-3-opus alias
MODEL_DEEPSEEK = "deepseek/deepseek-chat-v3.1"
MODEL_QWEN = "qwen/qwen3-235b-a22b-2507"           # qwen3-max alias on OpenRouter
MODEL_GROK = "x-ai/grok-4-fast"                    # closest OR alias to Grok-4.20


SYSTEM_PROMPT = """You are a perpetual-futures trading scout. Read the briefing JSON \
and output 0 to 3 high-conviction trade ideas.

Rules:
- Only act on strong setups. If nothing stands out, return [] (empty array).
- Each idea: {"symbol": str, "direction": "LONG"|"SHORT", "conviction": int 1-10, \
"thesis": str <= 50 words, "time_horizon_hours": int}
- Output ONLY the JSON array. No prose, no markdown fences, no explanation.
- Symbols must come from the briefing's funding_per_symbol list.
- Be selective: 1-2 ideas at conviction 6+ is better than 3 at conviction 4."""


def _call_openrouter(model: str, briefing: dict,
                     timeout: float = 30.0) -> Optional[str]:
    api_key = os.getenv("OPEN_ROUTER")
    if not api_key:
        logger.warning("OPEN_ROUTER env var not set; LLM client returns []")
        return None

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(briefing, sort_keys=True)},
        ],
        "max_tokens": 1024,
        "temperature": 0.3,
    }
    try:
        r = requests.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/pacifica-trading-bot/paper_sim",
                "X-Title": "paper_sim Account C",
            },
            json=payload,
            timeout=timeout,
        )
        if r.status_code != 200:
            logger.warning(f"openrouter {model} returned {r.status_code}: {r.text[:200]}")
            return None
        data = r.json()
        return data["choices"][0]["message"]["content"]
    except (requests.RequestException, KeyError, ValueError) as e:
        logger.warning(f"openrouter {model} call failed: {e}")
        return None


def _parse_ideas(raw: str) -> List[LLMTradeIdea]:
    """Tolerant JSON parser — strips markdown fences, finds first [...] in text."""
    if not raw:
        return []
    cleaned = raw.strip()
    # Strip ```json ... ``` fences if present
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"```\s*$", "", cleaned)
    # If model wrote prose around the JSON, find the first [...] block
    if not cleaned.startswith("["):
        m = re.search(r"\[[\s\S]*\]", cleaned)
        if not m:
            return []
        cleaned = m.group(0)
    try:
        items = json.loads(cleaned)
    except json.JSONDecodeError:
        return []
    if not isinstance(items, list):
        return []

    out: List[LLMTradeIdea] = []
    for it in items[:3]:
        if not isinstance(it, dict):
            continue
        try:
            sym = str(it["symbol"]).strip()
            direction = str(it["direction"]).upper().strip()
            if direction not in ("LONG", "SHORT"):
                continue
            conv = int(it["conviction"])
            if conv < 1 or conv > 10:
                continue
            thesis = str(it.get("thesis", "")).strip()[:200]
            horizon = float(it.get("time_horizon_hours", 24))
            out.append(LLMTradeIdea(
                symbol=sym, direction=direction, conviction=conv,
                thesis=thesis, time_horizon_hours=horizon,
            ))
        except (KeyError, ValueError, TypeError):
            continue
    return out


def call_with_meta(model: str, briefing: dict) -> dict:
    """Call OpenRouter and return full metadata: raw text, parsed ideas, error.

    Used by both make_client (parsed-only path) and make_logged_client
    (logging path) so they share one network call shape.
    """
    raw = _call_openrouter(model, briefing)
    if raw is None:
        return {"model": model, "raw": None, "parsed": [], "error": "no_response"}
    parsed = _parse_ideas(raw)
    return {"model": model, "raw": raw, "parsed": parsed,
            "error": None if parsed or not raw.strip() else "parse_returned_empty"}


def make_client(model: str):
    """Build a callable LLMClient for the given model id (no logging)."""
    def client(briefing: dict) -> List[LLMTradeIdea]:
        return call_with_meta(model, briefing)["parsed"]
    client.__name__ = f"llm_client[{model}]"
    return client


def make_logged_client(model: str, decision_log, cycle_id_provider=None):
    """Build a callable LLMClient that also writes raw response + parsed ideas
    to a DecisionLog every call.

    cycle_id_provider: callable returning a string cycle id (set by caller before
    each set of consensus calls). If None, each call gets a fresh uuid.
    """
    import uuid

    def client(briefing: dict) -> List[LLMTradeIdea]:
        meta = call_with_meta(model, briefing)
        cid = cycle_id_provider() if cycle_id_provider else str(uuid.uuid4())
        decision_log.append_llm_call(
            briefing=briefing, model=meta["model"],
            raw_response=meta["raw"], parsed_ideas=meta["parsed"],
            error=meta["error"], cycle_id=cid,
        )
        return meta["parsed"]
    client.__name__ = f"logged_client[{model}]"
    return client


# Pre-built clients
opus_client = make_client(MODEL_OPUS)
deepseek_client = make_client(MODEL_DEEPSEEK)
qwen_client = make_client(MODEL_QWEN)
grok_client = make_client(MODEL_GROK)


CLIENTS_BY_NAME = {
    "opus": opus_client,
    "deepseek": deepseek_client,
    "qwen": qwen_client,
    "grok": grok_client,
}
