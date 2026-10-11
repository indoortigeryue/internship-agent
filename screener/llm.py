"""Thin provider layer: one call() for Claude and GPT that returns a validated
Pydantic object plus a trace record (tokens, $ cost, latency).

Every response is cached on disk keyed by the full request, so re-running an
experiment costs nothing and every config starts from byte-identical
first-round extractions (a controlled comparison, not a re-roll).
"""

import hashlib
import json
import threading
import time
from pathlib import Path

import anthropic
import openai

CACHE = Path(__file__).resolve().parent / "data" / "llm_cache"

# USD per 1M tokens (input, output). Claude: Anthropic price list, 2026-10.
# GPT: third-party aggregators, 2026-09 — check openai.com/api/pricing.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
    "gpt-5.5": (5.00, 30.00),
    "gpt-5.4": (2.50, 15.00),
    "gpt-5.4-mini": (0.75, 4.50),
}

_clients = {}
_lock = threading.Lock()


def _client(provider):
    with _lock:
        if provider not in _clients:
            _clients[provider] = anthropic.Anthropic() if provider == "claude" else openai.OpenAI()
        return _clients[provider]


def provider_of(model):
    return "claude" if model.startswith("claude") else "openai"


def _call_claude(model, system, user, schema, effort):
    resp = _client("claude").messages.parse(
        model=model,
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_format=schema,
        output_config={"effort": effort},
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError(f"refused: {resp.stop_details}")
    return resp.parsed_output, resp.usage.input_tokens, resp.usage.output_tokens


def _call_openai(model, system, user, schema, effort):
    resp = _client("openai").responses.parse(
        model=model,
        instructions=system,
        input=user,
        text_format=schema,
        reasoning={"effort": effort},
    )
    return resp.output_parsed, resp.usage.input_tokens, resp.usage.output_tokens


def call(model, system, user, schema, effort="low"):
    """Returns (parsed: schema, trace: dict)."""
    key = hashlib.sha256(json.dumps(
        [model, system, user, schema.__name__, json.dumps(schema.model_json_schema(), sort_keys=True), effort]
    ).encode()).hexdigest()[:24]
    path = CACHE / f"{key}.json"
    if path.exists():
        hit = json.loads(path.read_text(encoding="utf-8"))
        return schema.model_validate(hit["output"]), {**hit["trace"], "cache_hit": True}

    fn = _call_claude if provider_of(model) == "claude" else _call_openai
    t0 = time.time()
    parsed, tin, tout = fn(model, system, user, schema, effort)
    pin, pout = PRICES.get(model, (0, 0))
    trace = {"model": model, "in_tokens": tin, "out_tokens": tout,
             "cost": round((tin * pin + tout * pout) / 1e6, 6),
             "latency_s": round(time.time() - t0, 2), "cache_hit": False}
    CACHE.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"output": parsed.model_dump(), "trace": trace}), encoding="utf-8")
    return parsed, trace
