"""Voice catalog — the browsable superset of TTS voices the operator can
approve in the Catalog tab (the "available to approve" source, analogous to
model_catalog.MODEL_CATALOG for models). Multi-provider, snapshotted from the
agent's backend/voice/tts_voices.yaml so the console can browse without a live
instance. `eu_resident` marks voices whose audio stays in the EU.

Two consumers:
  1. voice_registry._seed_registry() seeds the approved allow-list from the
     EU-resident entries here (safe-by-default: only EU voices pre-approved).
  2. The Catalog tab browses this list (GET /api/voices/catalog) so the operator
     can approve/curate which voices the Pipeline may then select from.

Curation happens in the Catalog; the Pipeline only SELECTS approved voices.
Regenerate from tts_voices.yaml when the agent's voice list changes.
"""
from __future__ import annotations

from typing import Any

VOICE_CATALOG: list[dict[str, Any]] = [
    {"id": "en-GB-Neural2-A", "provider": "google", "lang": "en-GB", "tier": "Neural2", "gender": "female", "label": "A (Female, Neural2)", "eu_resident": True},
    {"id": "en-GB-Neural2-B", "provider": "google", "lang": "en-GB", "tier": "Neural2", "gender": "male", "label": "B (Male, Neural2)", "eu_resident": True},
    {"id": "en-GB-Neural2-C", "provider": "google", "lang": "en-GB", "tier": "Neural2", "gender": "female", "label": "C (Female, Neural2)", "eu_resident": True},
    {"id": "en-GB-Neural2-D", "provider": "google", "lang": "en-GB", "tier": "Neural2", "gender": "male", "label": "D (Male, Neural2)", "eu_resident": True},
    {"id": "en-GB-Neural2-F", "provider": "google", "lang": "en-GB", "tier": "Neural2", "gender": "female", "label": "F (Female, Neural2)", "eu_resident": True},
    {"id": "en-GB-Neural2-N", "provider": "google", "lang": "en-GB", "tier": "Neural2", "gender": "female", "label": "N (Female, Neural2)", "eu_resident": True},
    {"id": "en-GB-Neural2-O", "provider": "google", "lang": "en-GB", "tier": "Neural2", "gender": "male", "label": "O (Male, Neural2)", "eu_resident": True},
    {"id": "en-GB-Studio-B", "provider": "google", "lang": "en-GB", "tier": "Studio", "gender": "male", "label": "B (Male, Studio)", "eu_resident": True},
    {"id": "en-GB-Studio-C", "provider": "google", "lang": "en-GB", "tier": "Studio", "gender": "female", "label": "C (Female, Studio)", "eu_resident": True},
    {"id": "en-US-Neural2-A", "provider": "google", "lang": "en-US", "tier": "Neural2", "gender": "male", "label": "A (Male, Neural2)", "eu_resident": True},
    {"id": "en-US-Neural2-C", "provider": "google", "lang": "en-US", "tier": "Neural2", "gender": "female", "label": "C (Female, Neural2)", "eu_resident": True},
    {"id": "en-US-Neural2-D", "provider": "google", "lang": "en-US", "tier": "Neural2", "gender": "male", "label": "D (Male, Neural2)", "eu_resident": True},
    {"id": "en-US-Neural2-E", "provider": "google", "lang": "en-US", "tier": "Neural2", "gender": "female", "label": "E (Female, Neural2)", "eu_resident": True},
    {"id": "en-US-Neural2-F", "provider": "google", "lang": "en-US", "tier": "Neural2", "gender": "female", "label": "F (Female, Neural2)", "eu_resident": True, "default": True},
    {"id": "en-US-Neural2-G", "provider": "google", "lang": "en-US", "tier": "Neural2", "gender": "female", "label": "G (Female, Neural2)", "eu_resident": True},
    {"id": "en-US-Neural2-H", "provider": "google", "lang": "en-US", "tier": "Neural2", "gender": "female", "label": "H (Female, Neural2)", "eu_resident": True},
    {"id": "en-US-Neural2-I", "provider": "google", "lang": "en-US", "tier": "Neural2", "gender": "male", "label": "I (Male, Neural2)", "eu_resident": True},
    {"id": "en-US-Neural2-J", "provider": "google", "lang": "en-US", "tier": "Neural2", "gender": "male", "label": "J (Male, Neural2)", "eu_resident": True},
    {"id": "en-US-Studio-O", "provider": "google", "lang": "en-US", "tier": "Studio", "gender": "female", "label": "O (Female, Studio)", "eu_resident": True},
    {"id": "en-US-Studio-Q", "provider": "google", "lang": "en-US", "tier": "Studio", "gender": "male", "label": "Q (Male, Studio)", "eu_resident": True},
    {"id": "es-ES-Neural2-A", "provider": "google", "lang": "es-ES", "tier": "Neural2", "gender": "female", "label": "A (Female, Neural2)", "eu_resident": True, "default": True},
    {"id": "es-ES-Neural2-E", "provider": "google", "lang": "es-ES", "tier": "Neural2", "gender": "female", "label": "E (Female, Neural2)", "eu_resident": True},
    {"id": "es-ES-Neural2-F", "provider": "google", "lang": "es-ES", "tier": "Neural2", "gender": "male", "label": "F (Male, Neural2)", "eu_resident": True},
    {"id": "es-ES-Neural2-G", "provider": "google", "lang": "es-ES", "tier": "Neural2", "gender": "male", "label": "G (Male, Neural2)", "eu_resident": True},
    {"id": "es-ES-Neural2-H", "provider": "google", "lang": "es-ES", "tier": "Neural2", "gender": "female", "label": "H (Female, Neural2)", "eu_resident": True},
    {"id": "es-ES-Studio-C", "provider": "google", "lang": "es-ES", "tier": "Studio", "gender": "female", "label": "C (Female, Studio)", "eu_resident": True},
    {"id": "es-ES-Studio-F", "provider": "google", "lang": "es-ES", "tier": "Studio", "gender": "male", "label": "F (Male, Studio)", "eu_resident": True},
    {"id": "es-US-Neural2-A", "provider": "google", "lang": "es-US", "tier": "Neural2", "gender": "female", "label": "A (Female, Neural2)", "eu_resident": True},
    {"id": "es-US-Neural2-B", "provider": "google", "lang": "es-US", "tier": "Neural2", "gender": "male", "label": "B (Male, Neural2)", "eu_resident": True},
    {"id": "es-US-Neural2-C", "provider": "google", "lang": "es-US", "tier": "Neural2", "gender": "male", "label": "C (Male, Neural2)", "eu_resident": True},
    {"id": "es-US-Studio-B", "provider": "google", "lang": "es-US", "tier": "Studio", "gender": "male", "label": "B (Male, Studio)", "eu_resident": True},
    {"id": "en-US-Chirp3-HD-Achernar", "provider": "google", "lang": "en-US", "tier": "Chirp3-HD", "gender": "female", "label": "Achernar (Female, Chirp3-HD)", "eu_resident": False},
    {"id": "en-US-Chirp3-HD-Achird", "provider": "google", "lang": "en-US", "tier": "Chirp3-HD", "gender": "male", "label": "Achird (Male, Chirp3-HD)", "eu_resident": False},
    {"id": "en-US-Chirp3-HD-Algenib", "provider": "google", "lang": "en-US", "tier": "Chirp3-HD", "gender": "male", "label": "Algenib (Male, Chirp3-HD)", "eu_resident": False},
    {"id": "en-US-Chirp3-HD-Algieba", "provider": "google", "lang": "en-US", "tier": "Chirp3-HD", "gender": "male", "label": "Algieba (Male, Chirp3-HD)", "eu_resident": False},
    {"id": "en-US-Chirp3-HD-Alnilam", "provider": "google", "lang": "en-US", "tier": "Chirp3-HD", "gender": "male", "label": "Alnilam (Male, Chirp3-HD)", "eu_resident": False},
    {"id": "en-US-Chirp3-HD-Aoede", "provider": "google", "lang": "en-US", "tier": "Chirp3-HD", "gender": "female", "label": "Aoede (Female, Chirp3-HD)", "eu_resident": False},
    {"id": "en-US-Chirp3-HD-Autonoe", "provider": "google", "lang": "en-US", "tier": "Chirp3-HD", "gender": "female", "label": "Autonoe (Female, Chirp3-HD)", "eu_resident": False},
    {"id": "en-US-Chirp3-HD-Callirrhoe", "provider": "google", "lang": "en-US", "tier": "Chirp3-HD", "gender": "female", "label": "Callirrhoe (Female, Chirp3-HD)", "eu_resident": False},
    {"id": "en-US-Chirp3-HD-Charon", "provider": "google", "lang": "en-US", "tier": "Chirp3-HD", "gender": "male", "label": "Charon (Male, Chirp3-HD)", "eu_resident": False},
    {"id": "en-US-Chirp3-HD-Despina", "provider": "google", "lang": "en-US", "tier": "Chirp3-HD", "gender": "female", "label": "Despina (Female, Chirp3-HD)", "eu_resident": False},
    {"id": "en-US-Chirp3-HD-Enceladus", "provider": "google", "lang": "en-US", "tier": "Chirp3-HD", "gender": "male", "label": "Enceladus (Male, Chirp3-HD)", "eu_resident": False},
    {"id": "en-US-Chirp3-HD-Erinome", "provider": "google", "lang": "en-US", "tier": "Chirp3-HD", "gender": "female", "label": "Erinome (Female, Chirp3-HD)", "eu_resident": False},
    {"id": "en-US-Chirp3-HD-Fenrir", "provider": "google", "lang": "en-US", "tier": "Chirp3-HD", "gender": "male", "label": "Fenrir (Male, Chirp3-HD)", "eu_resident": False},
    {"id": "en-US-Chirp3-HD-Gacrux", "provider": "google", "lang": "en-US", "tier": "Chirp3-HD", "gender": "female", "label": "Gacrux (Female, Chirp3-HD)", "eu_resident": False},
    {"id": "en-US-Chirp3-HD-Iapetus", "provider": "google", "lang": "en-US", "tier": "Chirp3-HD", "gender": "male", "label": "Iapetus (Male, Chirp3-HD)", "eu_resident": False},
    {"id": "en-US-Chirp3-HD-Kore", "provider": "google", "lang": "en-US", "tier": "Chirp3-HD", "gender": "female", "label": "Kore (Female, Chirp3-HD)", "eu_resident": False},
    {"id": "en-US-Chirp3-HD-Laomedeia", "provider": "google", "lang": "en-US", "tier": "Chirp3-HD", "gender": "female", "label": "Laomedeia (Female, Chirp3-HD)", "eu_resident": False},
    {"id": "en-US-Chirp3-HD-Leda", "provider": "google", "lang": "en-US", "tier": "Chirp3-HD", "gender": "female", "label": "Leda (Female, Chirp3-HD)", "eu_resident": False},
    {"id": "en-US-Chirp3-HD-Orus", "provider": "google", "lang": "en-US", "tier": "Chirp3-HD", "gender": "male", "label": "Orus (Male, Chirp3-HD)", "eu_resident": False},
    {"id": "en-US-Chirp3-HD-Puck", "provider": "google", "lang": "en-US", "tier": "Chirp3-HD", "gender": "male", "label": "Puck (Male, Chirp3-HD)", "eu_resident": False},
    {"id": "en-US-Chirp3-HD-Pulcherrima", "provider": "google", "lang": "en-US", "tier": "Chirp3-HD", "gender": "female", "label": "Pulcherrima (Female, Chirp3-HD)", "eu_resident": False},
    {"id": "en-US-Chirp3-HD-Rasalgethi", "provider": "google", "lang": "en-US", "tier": "Chirp3-HD", "gender": "male", "label": "Rasalgethi (Male, Chirp3-HD)", "eu_resident": False},
    {"id": "en-US-Chirp3-HD-Sadachbia", "provider": "google", "lang": "en-US", "tier": "Chirp3-HD", "gender": "male", "label": "Sadachbia (Male, Chirp3-HD)", "eu_resident": False},
    {"id": "en-US-Chirp3-HD-Sadaltager", "provider": "google", "lang": "en-US", "tier": "Chirp3-HD", "gender": "male", "label": "Sadaltager (Male, Chirp3-HD)", "eu_resident": False},
    {"id": "en-US-Chirp3-HD-Schedar", "provider": "google", "lang": "en-US", "tier": "Chirp3-HD", "gender": "male", "label": "Schedar (Male, Chirp3-HD)", "eu_resident": False},
    {"id": "en-US-Chirp3-HD-Sulafat", "provider": "google", "lang": "en-US", "tier": "Chirp3-HD", "gender": "female", "label": "Sulafat (Female, Chirp3-HD)", "eu_resident": False},
    {"id": "en-US-Chirp3-HD-Umbriel", "provider": "google", "lang": "en-US", "tier": "Chirp3-HD", "gender": "male", "label": "Umbriel (Male, Chirp3-HD)", "eu_resident": False},
    {"id": "en-US-Chirp3-HD-Vindemiatrix", "provider": "google", "lang": "en-US", "tier": "Chirp3-HD", "gender": "female", "label": "Vindemiatrix (Female, Chirp3-HD)", "eu_resident": False},
    {"id": "en-US-Chirp3-HD-Zephyr", "provider": "google", "lang": "en-US", "tier": "Chirp3-HD", "gender": "female", "label": "Zephyr (Female, Chirp3-HD)", "eu_resident": False},
    {"id": "en-US-Chirp3-HD-Zubenelgenubi", "provider": "google", "lang": "en-US", "tier": "Chirp3-HD", "gender": "male", "label": "Zubenelgenubi (Male, Chirp3-HD)", "eu_resident": False},
    {"id": "es-ES-Chirp3-HD-Achernar", "provider": "google", "lang": "es-ES", "tier": "Chirp3-HD", "gender": "female", "label": "Achernar (Female, Chirp3-HD)", "eu_resident": False},
    {"id": "es-ES-Chirp3-HD-Achird", "provider": "google", "lang": "es-ES", "tier": "Chirp3-HD", "gender": "male", "label": "Achird (Male, Chirp3-HD)", "eu_resident": False},
    {"id": "es-ES-Chirp3-HD-Algenib", "provider": "google", "lang": "es-ES", "tier": "Chirp3-HD", "gender": "male", "label": "Algenib (Male, Chirp3-HD)", "eu_resident": False},
    {"id": "es-ES-Chirp3-HD-Algieba", "provider": "google", "lang": "es-ES", "tier": "Chirp3-HD", "gender": "male", "label": "Algieba (Male, Chirp3-HD)", "eu_resident": False},
    {"id": "es-ES-Chirp3-HD-Alnilam", "provider": "google", "lang": "es-ES", "tier": "Chirp3-HD", "gender": "male", "label": "Alnilam (Male, Chirp3-HD)", "eu_resident": False},
    {"id": "es-ES-Chirp3-HD-Aoede", "provider": "google", "lang": "es-ES", "tier": "Chirp3-HD", "gender": "female", "label": "Aoede (Female, Chirp3-HD)", "eu_resident": False},
    {"id": "es-ES-Chirp3-HD-Autonoe", "provider": "google", "lang": "es-ES", "tier": "Chirp3-HD", "gender": "female", "label": "Autonoe (Female, Chirp3-HD)", "eu_resident": False},
    {"id": "es-ES-Chirp3-HD-Callirrhoe", "provider": "google", "lang": "es-ES", "tier": "Chirp3-HD", "gender": "female", "label": "Callirrhoe (Female, Chirp3-HD)", "eu_resident": False},
    {"id": "es-ES-Chirp3-HD-Charon", "provider": "google", "lang": "es-ES", "tier": "Chirp3-HD", "gender": "male", "label": "Charon (Male, Chirp3-HD)", "eu_resident": False},
    {"id": "es-ES-Chirp3-HD-Despina", "provider": "google", "lang": "es-ES", "tier": "Chirp3-HD", "gender": "female", "label": "Despina (Female, Chirp3-HD)", "eu_resident": False},
    {"id": "es-ES-Chirp3-HD-Enceladus", "provider": "google", "lang": "es-ES", "tier": "Chirp3-HD", "gender": "male", "label": "Enceladus (Male, Chirp3-HD)", "eu_resident": False},
    {"id": "es-ES-Chirp3-HD-Erinome", "provider": "google", "lang": "es-ES", "tier": "Chirp3-HD", "gender": "female", "label": "Erinome (Female, Chirp3-HD)", "eu_resident": False},
    {"id": "es-ES-Chirp3-HD-Fenrir", "provider": "google", "lang": "es-ES", "tier": "Chirp3-HD", "gender": "male", "label": "Fenrir (Male, Chirp3-HD)", "eu_resident": False},
    {"id": "es-ES-Chirp3-HD-Gacrux", "provider": "google", "lang": "es-ES", "tier": "Chirp3-HD", "gender": "female", "label": "Gacrux (Female, Chirp3-HD)", "eu_resident": False},
    {"id": "es-ES-Chirp3-HD-Iapetus", "provider": "google", "lang": "es-ES", "tier": "Chirp3-HD", "gender": "male", "label": "Iapetus (Male, Chirp3-HD)", "eu_resident": False},
    {"id": "es-ES-Chirp3-HD-Kore", "provider": "google", "lang": "es-ES", "tier": "Chirp3-HD", "gender": "female", "label": "Kore (Female, Chirp3-HD)", "eu_resident": False},
    {"id": "es-ES-Chirp3-HD-Laomedeia", "provider": "google", "lang": "es-ES", "tier": "Chirp3-HD", "gender": "female", "label": "Laomedeia (Female, Chirp3-HD)", "eu_resident": False},
    {"id": "es-ES-Chirp3-HD-Leda", "provider": "google", "lang": "es-ES", "tier": "Chirp3-HD", "gender": "female", "label": "Leda (Female, Chirp3-HD)", "eu_resident": False},
    {"id": "es-ES-Chirp3-HD-Orus", "provider": "google", "lang": "es-ES", "tier": "Chirp3-HD", "gender": "male", "label": "Orus (Male, Chirp3-HD)", "eu_resident": False},
    {"id": "es-ES-Chirp3-HD-Puck", "provider": "google", "lang": "es-ES", "tier": "Chirp3-HD", "gender": "male", "label": "Puck (Male, Chirp3-HD)", "eu_resident": False},
    {"id": "es-ES-Chirp3-HD-Pulcherrima", "provider": "google", "lang": "es-ES", "tier": "Chirp3-HD", "gender": "female", "label": "Pulcherrima (Female, Chirp3-HD)", "eu_resident": False},
    {"id": "es-ES-Chirp3-HD-Rasalgethi", "provider": "google", "lang": "es-ES", "tier": "Chirp3-HD", "gender": "male", "label": "Rasalgethi (Male, Chirp3-HD)", "eu_resident": False},
    {"id": "es-ES-Chirp3-HD-Sadachbia", "provider": "google", "lang": "es-ES", "tier": "Chirp3-HD", "gender": "male", "label": "Sadachbia (Male, Chirp3-HD)", "eu_resident": False},
    {"id": "es-ES-Chirp3-HD-Sadaltager", "provider": "google", "lang": "es-ES", "tier": "Chirp3-HD", "gender": "male", "label": "Sadaltager (Male, Chirp3-HD)", "eu_resident": False},
    {"id": "es-ES-Chirp3-HD-Schedar", "provider": "google", "lang": "es-ES", "tier": "Chirp3-HD", "gender": "male", "label": "Schedar (Male, Chirp3-HD)", "eu_resident": False},
    {"id": "es-ES-Chirp3-HD-Sulafat", "provider": "google", "lang": "es-ES", "tier": "Chirp3-HD", "gender": "female", "label": "Sulafat (Female, Chirp3-HD)", "eu_resident": False},
    {"id": "es-ES-Chirp3-HD-Umbriel", "provider": "google", "lang": "es-ES", "tier": "Chirp3-HD", "gender": "male", "label": "Umbriel (Male, Chirp3-HD)", "eu_resident": False},
    {"id": "es-ES-Chirp3-HD-Vindemiatrix", "provider": "google", "lang": "es-ES", "tier": "Chirp3-HD", "gender": "female", "label": "Vindemiatrix (Female, Chirp3-HD)", "eu_resident": False},
    {"id": "es-ES-Chirp3-HD-Zephyr", "provider": "google", "lang": "es-ES", "tier": "Chirp3-HD", "gender": "female", "label": "Zephyr (Female, Chirp3-HD)", "eu_resident": False},
    {"id": "es-ES-Chirp3-HD-Zubenelgenubi", "provider": "google", "lang": "es-ES", "tier": "Chirp3-HD", "gender": "male", "label": "Zubenelgenubi (Male, Chirp3-HD)", "eu_resident": False},
    {"id": "82c99ee6-f932-423f-a4a3-d403c8914b8d", "provider": "mistral", "lang": "en-GB", "tier": "Preset", "gender": "female", "label": "Jane - Neutral (Female, Preset)", "eu_resident": True},
    {"id": "3e617882-93de-4011-9e7f-44e27f4e7239", "provider": "mistral", "lang": "en-GB", "tier": "Preset", "gender": "male", "label": "Paul - Sad (Male, Preset)", "eu_resident": True},
    {"id": "Magpie-Multilingual.ES-US.Isabela", "provider": "nvidia", "lang": "es-US", "tier": "Neural", "gender": "female", "label": "Isabela (Female, Neural)", "eu_resident": False},
    {"id": "Magpie-Multilingual.ES-US.Diego", "provider": "nvidia", "lang": "es-US", "tier": "Neural", "gender": "male", "label": "Diego (Male, Neural)", "eu_resident": False},
    {"id": "Magpie-Multilingual.EN-US.Aria", "provider": "nvidia", "lang": "en-US", "tier": "Neural", "gender": "female", "label": "Aria (Female, Neural)", "eu_resident": False},
    {"id": "Magpie-Multilingual.EN-US.Jason", "provider": "nvidia", "lang": "en-US", "tier": "Neural", "gender": "male", "label": "Jason (Male, Neural)", "eu_resident": False},
    {"id": "es_ES-davefx-medium", "provider": "piper", "lang": "es-ES", "tier": "Medium", "gender": "male", "label": "Davefx (Male, Medium)", "eu_resident": True},
    {"id": "en_US-lessac-medium", "provider": "piper", "lang": "en-US", "tier": "Medium", "gender": "female", "label": "Lessac (Female, Medium)", "eu_resident": True},
    {"id": "Kore", "provider": "zenmux", "lang": "multi", "tier": "Gemini", "gender": "female", "label": "Kore (Female, Gemini)", "eu_resident": False},
    {"id": "Puck", "provider": "zenmux", "lang": "multi", "tier": "Gemini", "gender": "male", "label": "Puck (Male, Gemini)", "eu_resident": False},
    {"id": "Charon", "provider": "zenmux", "lang": "multi", "tier": "Gemini", "gender": "male", "label": "Charon (Male, Gemini)", "eu_resident": False},
]

VOICE_BY_ID: dict[str, dict[str, Any]] = {v["id"]: v for v in VOICE_CATALOG}

TIER_ORDER = ["Chirp3-HD", "Neural2", "Studio", "Wavenet", "Standard",
              "News", "Casual", "Polyglot", "Preset", "Neural", "Medium", "Gemini"]


def voices_for(provider: str | None = None,
               eu_resident: bool | None = None) -> list[dict[str, Any]]:
    """Browsable catalog subset, optionally filtered by provider and/or
    eu_resident (True = only EU-resident voices)."""
    out = VOICE_CATALOG
    if provider:
        out = [v for v in out if v["provider"] == provider]
    if eu_resident is not None:
        out = [v for v in out if bool(v.get("eu_resident")) == eu_resident]
    return list(out)
