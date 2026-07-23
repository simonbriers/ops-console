"""Finding 2.3 — approved-voice display/GDPR metadata (eu_resident/label/…) is
resolved from voice_catalog at read time, so a catalog correction reaches
already-approved voices instead of going stale in voices.json. Voices not in
the catalog (live-browsed) keep their stored metadata."""
import pytest

from backend import voice_registry as vr, voice_catalog


@pytest.fixture(autouse=True)
def _tmp_voices(tmp_path, monkeypatch):
    monkeypatch.setattr(vr, "_voices_path", lambda: tmp_path / "voices.json")
    yield


def test_eu_resident_is_resolved_from_catalog_not_the_snapshot(monkeypatch):
    vid = voice_catalog.VOICE_CATALOG[0]["id"]        # a catalog voice (EU-resident)
    prov = voice_catalog.VOICE_BY_ID[vid]["provider"]
    vr.approve_voice(vid, prov)                        # snapshots eu_resident=True
    # a later catalog correction flips it — list_registry must reflect the new value
    monkeypatch.setitem(voice_catalog.VOICE_BY_ID[vid], "eu_resident", False)
    got = next(v for v in vr.list_registry() if v["id"] == vid)
    assert got["eu_resident"] is False


def test_non_catalog_voice_keeps_stored_metadata():
    vr.approve_voice("custom-live-xyz", "mistral",
                     meta={"eu_resident": True, "label": "Custom", "lang": "en"})
    got = next(v for v in vr.list_registry() if v["id"] == "custom-live-xyz")
    assert got["eu_resident"] is True and got["label"] == "Custom"
