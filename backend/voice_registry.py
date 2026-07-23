"""Operator-controlled VOICE registry — the voice-side analogue of
model_registry.py. It is the missing middle layer between "approve the TTS
PROVIDER" (Catalog, model_registry / voice-only approval) and "pick the voice
for this clinic" (Pipeline).

Layering (operator decision, 2026-07):
  * Provider approval  — model_registry (approve `google` voice-only for TTS).
  * VOICE allow-list   — THIS module: which specific voices within an approved
    provider the operator permits. Curation + GDPR live here: seeded from
    voice_catalog's EU-resident voices (safe by default), grown/trimmed by the
    operator in the Catalog tab.
  * VOICE selection    — Pipeline: per clinic, pick ONE approved voice for
    voice.tts.voice_es / _en. The Pipeline offers ONLY registry voices, so an
    operator can't put a non-approved (e.g. non-EU) voice on a clinic.

Storage: <config-dir>/voices.json — same persistent location + trust class as
models.json / vault.json / clients.json. Never committed. Registry entries carry
provider/lang/gender/tier/label/eu_resident so the Pipeline can render + filter
without re-deriving anything.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from backend import voice_catalog
from backend.config import DEFAULT_CONFIG_PATH

# Fields copied verbatim from a catalog/browse entry onto a registry entry.
_VOICE_FIELDS = ("id", "provider", "lang", "gender", "tier", "label", "eu_resident")


def _voices_path() -> Path:
    return Path(DEFAULT_CONFIG_PATH).parent / "voices.json"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _seed_registry() -> list[dict[str, Any]]:
    """Safe-by-default seed: approve exactly the EU-resident voices from the
    catalog. Non-EU voices (e.g. Chirp3-HD) start UN-approved — the operator
    opts into them deliberately in the Catalog tab."""
    out = []
    for v in voice_catalog.voices_for(eu_resident=True):
        entry = {k: v.get(k) for k in _VOICE_FIELDS}
        entry["source"] = "builtin"
        entry["added"] = _now()
        out.append(entry)
    return out


def load_voices() -> dict[str, Any]:
    p = _voices_path()
    if not p.exists():
        data = {"version": 1, "registry": _seed_registry()}
        save_voices(data)
        return data
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        data.setdefault("registry", [])
        data.setdefault("version", 1)
        return data
    except (OSError, ValueError):
        return {"version": 1, "registry": []}


def save_voices(data: dict[str, Any]) -> None:
    _voices_path().write_text(json.dumps(data, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Registry — the operator voice allow-list
# ---------------------------------------------------------------------------

def list_registry(provider: str | None = None) -> list[dict[str, Any]]:
    """Approved voices, optionally filtered by provider. This is what the
    Pipeline's voice picker selects from.

    Display/GDPR metadata (lang/gender/tier/label/eu_resident) is RESOLVED from
    the catalog at read time for any voice that's in it, rather than trusted
    from the copy snapshotted into voices.json at approval time — so correcting
    a voice's eu_resident (or label) in voice_catalog reaches already-approved
    voices instead of silently going stale (2.3). Voices not in the catalog
    (e.g. live-browsed Mistral presets) keep their stored metadata."""
    out = []
    for v in load_voices()["registry"]:
        if provider and v.get("provider") != provider:
            continue
        entry = dict(v)
        cat = voice_catalog.VOICE_BY_ID.get(v.get("id"))
        if cat:
            for k in _VOICE_FIELDS:
                if cat.get(k) is not None:
                    entry[k] = cat[k]
        out.append(entry)
    return out


def approve_voice(voice_id: str, provider: str,
                  meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """Approve a voice (add it to the allow-list). Idempotent on id. Metadata
    (lang/gender/tier/label/eu_resident) comes from the static catalog when the
    voice is in it; for LIVE-browsed voices not in the catalog (e.g. Mistral
    presets pulled from the account), the caller passes `meta` with those
    fields so the id alone isn't required to be in the snapshot."""
    voice_id = (voice_id or "").strip()
    provider = (provider or "").strip()
    if not voice_id:
        return {"ok": False, "error": "need a voice id"}
    cat = voice_catalog.VOICE_BY_ID.get(voice_id)
    if cat:
        fields = {k: cat.get(k) for k in _VOICE_FIELDS}
    elif meta:
        fields = {"id": voice_id, "provider": provider or meta.get("provider"),
                  "lang": meta.get("lang"), "gender": meta.get("gender"),
                  "tier": meta.get("tier"), "label": meta.get("label") or voice_id,
                  "eu_resident": bool(meta.get("eu_resident"))}
    else:
        return {"ok": False, "error": f"voice {voice_id!r} is not in the catalog "
                "(and no metadata was supplied)"}
    if provider and fields.get("provider") and fields["provider"] != provider:
        return {"ok": False, "error": f"voice {voice_id!r} is a {fields.get('provider')} "
                f"voice, not {provider}"}
    data = load_voices()
    for v in data["registry"]:
        if v["id"] == voice_id:
            v.update(fields)  # refresh metadata
            save_voices(data)
            return {"ok": True, "error": None, "id": voice_id, "updated": True}
    entry = dict(fields)
    entry["source"] = "operator"
    entry["added"] = _now()
    data["registry"].append(entry)
    save_voices(data)
    return {"ok": True, "error": None, "id": voice_id, "updated": False}


def approved_ids() -> set[str]:
    """The set of approved voice ids — for the live browse to mark which voices
    are already on the allow-list."""
    return {v["id"] for v in load_voices()["registry"]}


def remove_voice(voice_id: str, provider: str | None = None) -> dict[str, Any]:
    data = load_voices()
    before = len(data["registry"])
    data["registry"] = [v for v in data["registry"]
                        if not (v["id"] == voice_id
                                and (provider is None or v.get("provider") == provider))]
    if len(data["registry"]) == before:
        return {"ok": False, "error": f"no approved voice {voice_id!r}"}
    save_voices(data)
    return {"ok": True, "error": None}


def browse(provider: str | None = None,
           eu_resident: bool | None = None) -> list[dict[str, Any]]:
    """The catalog superset annotated with whether each voice is already
    approved — the source for the Catalog tab's browse-and-approve UI."""
    approved = {v["id"] for v in load_voices()["registry"]}
    out = []
    for v in voice_catalog.voices_for(provider=provider, eu_resident=eu_resident):
        row = {k: v.get(k) for k in _VOICE_FIELDS}
        row["approved"] = v["id"] in approved
        out.append(row)
    return out
