"""Pure catalog tests (no DB, no network) — the selectable-model catalog and
its €/1k conversion, which the config picker and ledger.model_rates depend on.
ops-console has no shared pytest harness yet; this file is self-contained so it
runs the moment one exists (`pytest backend/tests/test_model_catalog.py`)."""
from backend import model_catalog as mc


def test_expected_models_present_and_no_nemo():
    ids = {m["id"] for m in mc.MODEL_CATALOG}
    assert "mistral-small-2506" in ids
    assert "mistral-large-2512" in ids
    assert "voxtral-mini-latest" in ids
    assert "voxtral-mini-transcribe-realtime-2602" in ids
    assert "voxtral-mini-tts-2603" in ids
    # nemo was abandoned (too many errors) — must not resurface as a choice
    assert not any("nemo" in i for i in ids)


def test_roles_and_units():
    small = mc.get_model("mistral-small-2506")
    assert small["role"] == "llm" and small["unit"] == "tokens"
    stt = mc.get_model("voxtral-mini-latest")
    assert stt["role"] == "stt" and stt["unit"] == "audio_minute"
    tts = mc.get_model("voxtral-mini-tts-2603")
    # the catch that started this: TTS meters per CHARACTER, not per minute
    assert tts["role"] == "tts" and tts["unit"] == "character"


def test_llm_per_1k_conversion():
    # €/1M ÷ 1000 = €/1k
    assert mc.llm_buy_rates_per_1k("mistral-small-2506") == (0.000085, 0.0000085, 0.000255)
    assert mc.llm_buy_rates_per_1k("mistral-large-2512") == (0.000425, 0.0000425, 0.001275)
    # non-LLM models have no token rate
    assert mc.llm_buy_rates_per_1k("voxtral-mini-tts-2603") is None
    assert mc.llm_buy_rates_per_1k("does-not-exist") is None


def test_filters_and_defaults():
    llm = mc.models_for(role="llm", provider="mistral")
    assert {m["id"] for m in llm} == {"mistral-small-2506", "mistral-large-2512"}
    assert mc.default_model("llm", "mistral") == "mistral-small-2506"
    assert mc.default_model("stt", "mistral") == "voxtral-mini-latest"
    # a provider with no catalog entries yields nothing (picker falls to free text)
    assert mc.models_for(role="llm", provider="nvidia") == []
    assert mc.default_model("llm", "nvidia") is None


def test_zenmux_free_models_and_tts_registered():
    # ZenMux free-tier LLM trials are provider-filterable for the picker; none
    # is the fleet default, so default_model falls to the first candidate.
    zen_llm = mc.models_for(role="llm", provider="zenmux")
    assert {m["id"] for m in zen_llm} == {
        "moonshotai/kimi-k3-free", "z-ai/glm-4.6v-flash-free", "z-ai/glm-4.7-flash-free"}
    assert all(m["buy_in_per_m"] == 0.0 for m in zen_llm)  # free = zero buy cost
    assert mc.default_model("llm", "zenmux") == "moonshotai/kimi-k3-free"
    # The paid, dev-only Gemini TTS model is registered under the tts role.
    zen_tts = mc.models_for(role="tts", provider="zenmux")
    assert {m["id"] for m in zen_tts} == {"google/gemini-3.1-flash-tts-preview"}
    assert zen_tts[0]["unit"] == "character"


def test_llm_models_per_1k_covers_only_llm():
    rows = mc.llm_models_per_1k()
    # Every LLM model (incl. the ZenMux free trials, priced at 0) — but no
    # STT/TTS entries, which have no per-token rate.
    assert {r["id"] for r in rows} == {
        "mistral-small-2506", "mistral-large-2512",
        "moonshotai/kimi-k3-free", "z-ai/glm-4.6v-flash-free", "z-ai/glm-4.7-flash-free"}
    small = next(r for r in rows if r["id"] == "mistral-small-2506")
    assert small["buy_in"] == 0.000085 and small["buy_out"] == 0.000255
    kimi = next(r for r in rows if r["id"] == "moonshotai/kimi-k3-free")
    assert kimi["buy_in"] == 0.0 and kimi["buy_out"] == 0.0
