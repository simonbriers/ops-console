"""Canonical provider registry — the SINGLE source for "which providers are
valid for each role" (LLM / STT / TTS).

Before this, that list was hand-copied into four places that could drift:
  - frontend/catalog.js   CAT_ROLES[].browse / .voiceOnly, CAT_VOICE_PROVIDERS
  - frontend/pipeline.js  PL_LANES[].providers
  - frontend/setup.js     SU_BROWSE_PROVIDERS
  - backend/config_manager.py  the llm_provider field's `options`
The membership agreed across all four when this was extracted (only ordering
and the browse-vs-voice-only split differed); this module makes that agreement
enforced instead of coincidental. Served to the frontend at GET /api/providers.

Distinct from vault.KIND_META (`/api/vault/kinds`): that covers only
credential-bearing providers and their .env key names. This registry also
covers keyless/local providers (ollama) and voice-only TTS engines
(google, piper) that have no single API key — because selectability is a
different question from "does it need a key." Credential/env metadata still
comes from the vault; role selectability comes from here.

`voice_only` (per role): the provider has no browsable *model* for that role —
its voice/engine itself is the pick (google/piper TTS). `local`: runs on the
box, needs no credential (ollama, piper).
"""
from __future__ import annotations

from typing import Any

ROLES = ("llm", "stt", "tts")

# Ordered — per-role order below is the order each role's list is served in.
_PROVIDERS: list[dict[str, Any]] = [
    {"name": "mistral",    "label": "Mistral · Voxtral",  "roles": ["llm", "stt", "tts"]},
    {"name": "nvidia",     "label": "NVIDIA",             "roles": ["llm", "tts"]},
    {"name": "openrouter", "label": "OpenRouter",         "roles": ["llm"]},
    {"name": "zenmux",     "label": "ZenMux (aggregator)", "roles": ["llm", "tts"]},
    {"name": "ollama",     "label": "Ollama (local)",     "roles": ["llm"], "local": True},
    {"name": "google",     "label": "Google Cloud TTS",   "roles": ["tts"], "voice_only": ["tts"]},
    {"name": "piper",      "label": "Piper (local)",      "roles": ["tts"], "voice_only": ["tts"], "local": True},
]


def _entry(p: dict[str, Any], role: str) -> dict[str, Any]:
    return {
        "name": p["name"],
        "label": p.get("label", p["name"]),
        "voice_only": role in p.get("voice_only", []),
        "local": bool(p.get("local")),
    }


def providers_for(role: str) -> list[dict[str, Any]]:
    """Ordered providers valid for a role, each tagged voice_only/local."""
    return [_entry(p, role) for p in _PROVIDERS if role in p["roles"]]


def names_for(role: str) -> list[str]:
    """Just the provider names for a role, in order (e.g. the llm_provider
    select options)."""
    return [e["name"] for e in providers_for(role)]


def by_role() -> dict[str, list[dict[str, Any]]]:
    """{role: [ {name,label,voice_only,local}, ... ]} — the /api/providers
    payload the frontend builds its per-role lists from."""
    return {role: providers_for(role) for role in ROLES}
