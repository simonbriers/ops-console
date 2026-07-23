"""Finding 4.3 — the id-key comma-split that turns a stored set into its metrics
alias(es) now lives once, in vault.set_aliases / vault.alias_to_set_map. list_sets
and the ledger's alias->set join both call it instead of re-implementing the
split off vault's private _ID_KEY. These pin that shared behavior."""
import pytest

from backend import vault


@pytest.fixture(autouse=True)
def _tmp_vault(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "_vault_path", lambda: tmp_path / "vault.json")
    yield


def test_set_aliases_single_multi_and_nonkey():
    single = {"kind": "mistral", "values": {"MISTRAL_API_KEY": "abc"}}
    assert vault.set_aliases(single) == [vault.alias_for("abc")]
    # primary+fallback pair -> one alias per key, in order
    pair = {"kind": "openrouter", "values": {"OPENROUTER_API_KEY": "k1, k2"}}
    assert vault.set_aliases(pair) == [vault.alias_for("k1"), vault.alias_for("k2")]
    # non-key kinds meter under nothing
    assert vault.set_aliases({"kind": "smtp", "values": {"SMTP_HOST": "h"}}) == []
    assert vault.set_aliases({"kind": "file/google_tts", "values": {}}) == []


def test_list_sets_uses_the_shared_helper():
    r = vault.upsert_set("m", "mistral", {"MISTRAL_API_KEY": "key1,key2"})
    s = next(x for x in vault.list_sets() if x["id"] == r["id"])
    assert s["aliases"] == [vault.alias_for("key1"), vault.alias_for("key2")]
    assert s["key_count"] == 2
    assert s["alias"] == vault.alias_for("key1")


def test_alias_to_set_map_and_ledger_delegates():
    r = vault.upsert_set("m", "mistral", {"MISTRAL_API_KEY": "key1,key2"})
    amap = vault.alias_to_set_map()
    assert amap[vault.alias_for("key1")]["id"] == r["id"]
    assert amap[vault.alias_for("key2")]["id"] == r["id"]
    # the ledger's _alias_map must be the same mapping (it delegates now)
    from backend import ledger
    assert set(ledger._alias_map().keys()) == set(amap.keys())
