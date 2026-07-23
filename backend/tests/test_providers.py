"""Tests for the canonical provider registry (backend/providers.py) — the
single source that replaced the per-role provider lists hand-copied into
catalog.js (CAT_ROLES/CAT_VOICE_PROVIDERS), pipeline.js (PL_LANES), setup.js
(SU_BROWSE_PROVIDERS) and config_manager's llm_provider options.

These pin the *membership* to what those lists contained when they were
collapsed, so the dedup is proven behavior-preserving (the four had agreed on
members; only order/tagging differed). Pure data, no network."""
from backend import providers


# The member SETS every one of the four hand-copied lists carried at collapse.
LEGACY = {
    "llm": {"mistral", "nvidia", "openrouter", "zenmux", "ollama"},
    "stt": {"mistral"},
    "tts": {"mistral", "nvidia", "zenmux", "google", "piper"},
}


def test_by_role_membership_matches_legacy_hardcoded_lists():
    by = providers.by_role()
    for role, want in LEGACY.items():
        assert {e["name"] for e in by[role]} == want, role


def test_names_for_matches_membership():
    for role, want in LEGACY.items():
        assert set(providers.names_for(role)) == want


def test_voice_only_split_reproduces_catalog_browse_vs_voiceonly():
    by = providers.by_role()
    # catalog.js TTS: browse = mistral/zenmux/nvidia, voiceOnly = google/piper
    tts_browse = {e["name"] for e in by["tts"] if not e["voice_only"]}
    tts_voice_only = {e["name"] for e in by["tts"] if e["voice_only"]}
    assert tts_browse == {"mistral", "nvidia", "zenmux"}
    assert tts_voice_only == {"google", "piper"}
    # llm/stt providers are never voice-only
    assert all(not e["voice_only"] for e in by["llm"])
    assert all(not e["voice_only"] for e in by["stt"])


def test_local_flag_marks_only_ollama_and_piper():
    by = providers.by_role()
    local = {e["name"] for role in providers.ROLES for e in by[role] if e["local"]}
    assert local == {"ollama", "piper"}


def test_config_manager_llm_options_come_from_registry():
    """The backend consumer (config_manager's llm_provider select) must build
    its options from this registry, not a private literal."""
    from backend import config_manager
    grp = next(g for g in config_manager.FIELD_GROUPS if g["key"] == "llm")
    fld = next(f for f in grp["fields"] if f["name"] == "llm_provider")
    assert fld["options"] == providers.names_for("llm")
