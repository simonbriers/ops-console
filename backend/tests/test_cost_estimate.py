"""Finding 2.2 — the per-client cost estimate is EUR (was mislabeled
`estimated_usd`) and reads its rates from the ledger PLAN (the € pricing
authority), falling back to the legacy clients.json cost_per_1k_* fields only
when a client has no plan yet."""
import pytest

from backend import core, ledger

USAGE = {"ok": True, "input_tokens": 1000, "cached_tokens": 0, "output_tokens": 1000}


@pytest.fixture(autouse=True)
def _tmp_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "_db_path", lambda: tmp_path / "ledger.sqlite")
    yield


def test_field_is_eur_and_unconfigured_is_none():
    r = core.compute_cost_estimate(USAGE, {"name": "NoPlan"})
    assert "estimated_eur" in r and "estimated_usd" not in r
    assert r["configured"] is False and r["estimated_eur"] is None


def test_falls_back_to_client_fields_without_a_plan():
    client = {"name": "Legacy", "cost_per_1k_input_tokens": 0.1,
              "cost_per_1k_output_tokens": 0.2}
    r = core.compute_cost_estimate(USAGE, client)
    assert r["configured"] is True
    assert r["estimated_eur"] == 0.3          # 1k/1k*0.1 + 1k/1k*0.2


def test_plan_sell_rates_win_over_client_fields():
    client = {"name": "WithPlan", "cost_per_1k_input_tokens": 0.1}
    ledger.ensure_plan(client)                        # seeds a plan row
    ledger.set_plan("WithPlan", {"sell_in": 0.5, "sell_cached": 0.0, "sell_out": 0.0})
    r = core.compute_cost_estimate(USAGE, client)
    assert r["estimated_eur"] == 0.5                  # plan's 0.5, not the client's 0.1
