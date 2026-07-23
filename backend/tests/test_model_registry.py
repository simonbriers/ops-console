"""Pure storage/governance tests for the operator model registry
(model_registry.py) — no SSH, no network. The instance-facing paths
(assign_model's write, reconcile_models' live read) are exercised only up to
the point they'd hit the network; the governance REJECTIONS (unapproved model,
wrong role, bad slot) all return before any I/O, so they're testable here.

Self-contained like test_model_catalog.py: points _models_path at a temp file
so it never touches the real <config-dir>/models.json."""
import pytest

from backend import model_registry as mr


@pytest.fixture(autouse=True)
def _tmp_models(tmp_path, monkeypatch):
    monkeypatch.setattr(mr, "_models_path", lambda: tmp_path / "models.json")
    yield


def test_registry_seeds_from_catalog():
    reg = mr.list_registry()
    ids = {m["id"] for m in reg}
    # built-in fleet is present, incl. the ZenMux free models added to the catalog
    assert "mistral-small-2506" in ids
    assert "moonshotai/kimi-k3-free" in ids
    # The registry is a price-free allow-list now (DUPLICATION_AUDIT 2.1): the
    # role is carried, but price fields are NOT — price lives once in
    # model_catalog / the ledger's model_rates, not copied onto registry entries.
    small = next(m for m in reg if m["id"] == "mistral-small-2506")
    assert small["role"] == "llm"
    assert "buy_in_per_m" not in small


def test_add_update_remove_model():
    r = mr.add_model("acme/new-llm", "zenmux", "llm", label="New LLM")
    assert r["ok"] and r["updated"] is False
    assert any(m["id"] == "acme/new-llm" for m in mr.list_registry(provider="zenmux"))
    # idempotent upsert on (id, provider)
    r2 = mr.add_model("acme/new-llm", "zenmux", "llm", notes="hi")
    assert r2["ok"] and r2["updated"] is True
    assert next(m for m in mr.list_registry() if m["id"] == "acme/new-llm")["notes"] == "hi"
    # bad role refused
    assert mr.add_model("x", "p", "bogus")["ok"] is False
    # remove
    assert mr.remove_model("acme/new-llm")["ok"] is True
    assert not any(m["id"] == "acme/new-llm" for m in mr.list_registry())
    assert mr.remove_model("acme/new-llm")["ok"] is False


def test_registry_role_filter():
    llm = mr.list_registry(role="llm")
    assert all(m["role"] == "llm" for m in llm)
    stt = mr.list_registry(role="stt")
    assert {m["id"] for m in stt} and all(m["role"] == "stt" for m in stt)


def test_assignment_record_and_list():
    mr.record_model_assignment("Acme", "llm", "mistral-small-2506", "assign")
    a = next(x for x in mr.list_model_assignments() if x["client"] == "Acme" and x["slot"] == "llm")
    assert a["model_id"] == "mistral-small-2506"
    assert a["in_registry"] is True and a["provider"] == "mistral"
    # one active per (client, slot): re-record replaces
    mr.record_model_assignment("Acme", "llm", "mistral-large-2512", "assign")
    llm_for_acme = [x for x in mr.list_model_assignments() if x["client"] == "Acme" and x["slot"] == "llm"]
    assert len(llm_for_acme) == 1 and llm_for_acme[0]["model_id"] == "mistral-large-2512"
    mr.clear_model_assignment("Acme", "llm")
    assert not any(x["client"] == "Acme" and x["slot"] == "llm" for x in mr.list_model_assignments())


def test_assign_model_governance_rejections():
    fake_client = {"name": "Acme", "ssh_target": "x", "remote_dir": "/y"}
    # unknown slot — refused before any write
    assert mr.assign_model(fake_client, "bogus", "mistral-small-2506")["ok"] is False
    # model not in the registry — the core governance guard
    r = mr.assign_model(fake_client, "llm", "totally/unknown-model")
    assert r["ok"] is False and "not an approved" in r["error"]
    # right model, wrong slot role: an STT model can't fill the text-llm slot
    stt_id = mr.list_registry(role="stt")[0]["id"]
    assert mr.assign_model(fake_client, "llm", stt_id)["ok"] is False
