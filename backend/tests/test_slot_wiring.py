"""Finding 3.2 — model_registry.SLOTS and _SLOT_YAML are now DERIVED from
config_manager.FIELD_GROUPS instead of being two hand-kept dicts that could
drift. These pin the derived values to what the two literals held before, and
prove the read-side YAML paths come from the same config field the write side
uses (so a typo can't make assign write one path while reconcile reads another)."""
from backend import model_registry as mr
from backend import config_manager


EXPECTED_SLOTS = {
    "llm":       {"field": "llm_model",       "role": "llm", "provider_field": "llm_provider"},
    "voice_llm": {"field": "voice_llm_model", "role": "llm", "provider_field": "voice_llm_provider"},
    "voice_stt": {"field": "voice_stt_model", "role": "stt", "provider_field": "voice_stt_provider"},
}
EXPECTED_SLOT_YAML = {
    "llm":       {"model": ("llm", "model"),          "provider": ("llm", "provider")},
    "voice_llm": {"model": ("voice", "llm", "model"), "provider": ("voice", "llm", "provider")},
    "voice_stt": {"model": ("voice", "stt", "model"), "provider": ("voice", "stt", "provider")},
}


def test_slots_match_legacy_values():
    assert mr.SLOTS == EXPECTED_SLOTS


def test_slot_yaml_matches_legacy_values():
    assert mr._SLOT_YAML == EXPECTED_SLOT_YAML


def test_yaml_paths_are_the_config_fields_own_paths():
    fields = {f["name"]: f for g in config_manager.FIELD_GROUPS for f in g["fields"]}
    for slot, spec in mr.SLOTS.items():
        # read-side model/provider paths must equal the config field's dotted path
        assert ".".join(mr._SLOT_YAML[slot]["model"]) == fields[spec["field"]]["path"]
        assert ".".join(mr._SLOT_YAML[slot]["provider"]) == fields[spec["provider_field"]]["path"]
