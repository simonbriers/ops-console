"""Model catalog (docs/TOKEN_ECONOMY_PLAN.md Part 6, item #9) — the single
source of truth for which provider models are *selectable* in the config
manager and what each one costs us to run (buy-side).

Two consumers:

1. **Config manager picker.** `config.js` renders a `type:"model"` field as a
   free-text box (so you can still test an unlisted model) whose autocomplete
   options and price hints come from this catalog, filtered by role + the
   provider chosen in the sibling field. This is what makes small/large (and
   the STT models) a real, priced menu instead of a typed string.

2. **Ledger per-model buy pricing.** `ledger.py` seeds its `model_rates`
   table from the LLM entries here (converted to €/1k) so the by-model usage
   it already snapshots can be priced per model rather than at one blended
   per-source rate. Voice entries are *registered* (correct native units,
   real prices) but not yet metered — voice metering is Phase 8, so their
   prices sit ready without pricing anything today (D1: native units).

Prices are BUY-side (what the provider charges us), in EUR, from Mistral's
public price list captured 2026-07-21. Native units differ by role — this is
D1 in the plan: LLM meters per token, STT per audio-minute, TTS per
character — so each role carries the price in the unit the provider bills in.
Editing a live rate is done in the ledger (`set_model_rate`); this file is
the DEFAULT catalog, seeded once (INSERT OR IGNORE), never overwriting an
operator edit.

nemo is deliberately absent: it was trialled as the conversational model,
made too many errors, and was abandoned — the fleet policy is small by
default, large only as a named exception.
"""
from __future__ import annotations

from typing import Any

# LLM prices are quoted per 1M tokens (how Mistral's page lists them); voice
# prices are per their own native unit. `default` marks the fleet workhorse
# a provider's picker preselects / a source-rate stamp uses.
MODEL_CATALOG: list[dict[str, Any]] = [
    {
        "id": "mistral-small-2506",
        "provider": "mistral",
        "role": "llm",
        "unit": "tokens",
        "label": "Mistral Small 3.2 (2506)",
        "default": True,
        "buy_in_per_m": 0.085,
        "buy_cached_per_m": 0.0085,
        "buy_out_per_m": 0.255,
        "notes": "Fleet workhorse — 2.25M tok/min, 5 req/s (free tier).",
    },
    {
        "id": "mistral-large-2512",
        "provider": "mistral",
        "role": "llm",
        "unit": "tokens",
        "label": "Mistral Large (2512)",
        "default": False,
        "buy_in_per_m": 0.425,
        "buy_cached_per_m": 0.0425,
        "buy_out_per_m": 1.275,
        "notes": "Named exception only — 250k tok/min, 0.07 req/s "
                 "(~1 req/14s); needs a key_2 fallback under concurrency.",
    },
    {
        "id": "voxtral-mini-latest",
        "provider": "mistral",
        "role": "stt",
        "unit": "audio_minute",
        "label": "Voxtral Mini (batch/latest STT)",
        "default": True,
        "buy_per_unit": 0.00255,
        "notes": "Speech-to-text, per audio-minute.",
    },
    {
        "id": "voxtral-mini-transcribe-realtime-2602",
        "provider": "mistral",
        "role": "stt",
        "unit": "audio_minute",
        "label": "Voxtral Mini Transcribe (realtime STT)",
        "default": False,
        "buy_per_unit": 0.0051,
        "notes": "Realtime STT — 2x the batch price, per audio-minute.",
    },
    {
        "id": "voxtral-mini-tts-2603",
        "provider": "mistral",
        "role": "tts",
        "unit": "character",
        "label": "Voxtral Mini TTS (2603)",
        "default": True,
        "buy_per_unit": 0.0000136,
        "notes": "Text-to-speech, per CHARACTER (~€13.6/M chars). Registered "
                 "for pricing; not metered until Phase 8.",
    },
]

MODEL_BY_ID: dict[str, dict[str, Any]] = {m["id"]: m for m in MODEL_CATALOG}

ROLES = ("llm", "stt", "tts")


def get_model(model_id: str) -> dict[str, Any] | None:
    return MODEL_BY_ID.get(model_id)


def models_for(role: str | None = None,
               provider: str | None = None) -> list[dict[str, Any]]:
    """Catalog subset filtered by role and/or provider (both optional)."""
    out = MODEL_CATALOG
    if role:
        out = [m for m in out if m["role"] == role]
    if provider:
        out = [m for m in out if m["provider"] == provider]
    return list(out)


def default_model(role: str, provider: str) -> str | None:
    """The model a provider's picker should preselect for a role."""
    candidates = models_for(role=role, provider=provider)
    for m in candidates:
        if m.get("default"):
            return m["id"]
    return candidates[0]["id"] if candidates else None


def llm_buy_rates_per_1k(model_id: str) -> tuple[float, float, float] | None:
    """(buy_in, buy_cached, buy_out) in €/1k tokens for an LLM model — the
    unit ledger.source_rates / model_rates store. None for non-LLM / unknown.
    Converts from the catalog's per-1M quote (÷1000)."""
    m = MODEL_BY_ID.get(model_id)
    if not m or m["role"] != "llm":
        return None
    return (round(m["buy_in_per_m"] / 1000, 9),
            round(m["buy_cached_per_m"] / 1000, 9),
            round(m["buy_out_per_m"] / 1000, 9))


def llm_models_per_1k() -> list[dict[str, Any]]:
    """Every LLM model as {id, provider, buy_in, buy_cached, buy_out} in €/1k
    — what ledger.py seeds model_rates from."""
    out = []
    for m in MODEL_CATALOG:
        rates = llm_buy_rates_per_1k(m["id"])
        if rates is None:
            continue
        out.append({"id": m["id"], "provider": m["provider"],
                    "buy_in": rates[0], "buy_cached": rates[1],
                    "buy_out": rates[2]})
    return out
