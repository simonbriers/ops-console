"""Pure tests for vault.list_kinds() — the Phase 0 single source the frontend
(credentials.js VAULT_FIELDS, catalog.js CAT_PROVIDERS, and the #vaultKindSelect
markup) now renders credential key fields from instead of hardcoding them.

No SSH, no network, no vault file: list_kinds() reads only module constants
(KIND_META / _ID_KEY), so nothing here touches <config-dir>/vault.json."""
from backend import vault


def test_list_kinds_covers_every_kind_meta_row():
    kinds = vault.list_kinds()
    by = {k["kind"]: k for k in kinds}
    # exactly the KIND_META kinds, nothing added or dropped
    assert set(by) == set(vault.KIND_META)
    # order is stable (KIND_META insertion order) so the UI select is stable
    assert [k["kind"] for k in kinds] == list(vault.KIND_META)


def test_list_kinds_shape_matches_kind_meta():
    by = {k["kind"]: k for k in vault.list_kinds()}
    for kind, meta in vault.KIND_META.items():
        row = by[kind]
        assert row["provider"] == meta["provider"]
        assert row["keys"] == list(meta["keys"])
        assert set(row["roles"]) == set(meta["roles"])
        assert row["role"] == meta["role"]
        assert row["label"]                      # every kind now carries a label
        assert row["id_key"] == vault._ID_KEY.get(kind)


def test_list_kinds_idkey_and_file_flags():
    by = {k["kind"]: k for k in vault.list_kinds()}
    # single-key llm-ish kinds expose the id_key the Catalog provider box uses
    for kind in ("mistral", "openrouter", "nvidia", "zenmux"):
        assert by[kind]["id_key"] == vault.KIND_KEYS[kind][0]
        assert by[kind]["is_file"] is False
    # multi-field creds have no single identifying key
    assert by["smtp"]["id_key"] is None
    assert by["twilio"]["id_key"] is None
    # the file credential: no typed keys, flagged is_file, no id_key
    g = by["file/google_tts"]
    assert g["is_file"] is True
    assert g["keys"] == []
    assert g["id_key"] is None


def test_list_kinds_keys_match_legacy_frontend_fallback():
    """The kind->env-keys map the endpoint yields must equal the hardcoded
    fallback still shipped in credentials.js (VAULT_FIELDS), so behaviour is
    identical whether or not the fetch succeeds."""
    by = {k["kind"]: k["keys"] for k in vault.list_kinds()}
    legacy = {
        "mistral": ["MISTRAL_API_KEY"],
        "openrouter": ["OPENROUTER_API_KEY"],
        "nvidia": ["NVIDIA_API_KEY"],
        "zenmux": ["ZENMUX_API_KEY"],
        "smtp": ["SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_USE_TLS"],
        "twilio": ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER"],
        "file/google_tts": [],
    }
    assert by == legacy
