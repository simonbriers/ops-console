"""Finding 2.1 — model_catalog is the single price owner. The ledger's
model_rates now REFRESH from the catalog on connect (so a catalog price fix
reaches billing) while preserving operator overrides; the model registry no
longer carries price fields at all."""
import pytest

from backend import ledger, model_registry as mr

MODEL = "mistral-small-2506"   # a catalog LLM model with a real buy_in


@pytest.fixture(autouse=True)
def _tmp_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "_db_path", lambda: tmp_path / "ledger.sqlite")
    monkeypatch.setattr(mr, "_models_path", lambda: tmp_path / "models.json")
    yield


def _rate(model):
    return {r["model"]: r for r in ledger.get_model_rates()}[model]


def _patch_catalog_price(monkeypatch, model, buy_in):
    orig = ledger.model_catalog.llm_models_per_1k

    def bumped():
        out = orig()
        for m in out:
            if m["id"] == model:
                m["buy_in"] = buy_in
        return out

    monkeypatch.setattr(ledger.model_catalog, "llm_models_per_1k", bumped)


def test_catalog_price_change_reaches_model_rates(monkeypatch):
    assert MODEL in _rate(MODEL)["model"]              # seeded on first connect
    _patch_catalog_price(monkeypatch, MODEL, 9.999)
    ledger.get_model_rates()                           # reconnect -> refresh
    assert _rate(MODEL)["buy_in"] == 9.999             # no longer frozen


def test_operator_override_survives_catalog_refresh(monkeypatch):
    ledger.set_model_rate(MODEL, "mistral", buy_in=0.5, buy_cached=0.05, buy_out=1.0)
    _patch_catalog_price(monkeypatch, MODEL, 9.999)    # catalog moves...
    ledger.get_model_rates()                           # ...refresh runs...
    assert _rate(MODEL)["buy_in"] == 0.5               # ...operator value kept


def test_registry_entries_have_no_price_fields():
    for entry in mr.list_registry():
        for pf in mr._PRICE_FIELDS:
            assert pf not in entry, f"{entry['id']} still carries {pf}"
