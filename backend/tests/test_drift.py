"""config_manager._missing_defaults — the shipped-vs-live "stale config" gap.

Regression for the 2026-07-23 false positive: the primary's live theme evolved
`custom_theme.--accent` from a flat string to a {dark, light} dict, and the
naive leaf comparison reported the shipped flat keys as "missing" even though
the live file was strictly richer. A scalar shipped key that exists live as a
nested subtree must NOT be flagged; genuinely-absent keys still must be."""
from backend import config_manager as cm


def test_scalar_default_expanded_to_subtree_is_not_missing():
    shipped = {
        "site.custom_theme.--accent": "#059669",         # flat in shipped
        "site.custom_theme.--bg": "#f6f3ef",
    }
    live = {                                             # {dark,light} live-side
        "site.custom_theme.--accent.dark": "#10b981",
        "site.custom_theme.--accent.light": "#059669",
        "site.custom_theme.--bg.dark": "#0e1f29",
        "site.custom_theme.--bg.light": "#f7f3ec",
    }
    assert cm._missing_defaults(shipped, live) == []      # no false positives


def test_genuinely_absent_key_is_still_flagged():
    shipped = {"site.timezone": "Europe/Madrid", "site.custom_theme.--accent": "#059669"}
    live = {"site.custom_theme.--accent.dark": "#10b981"}   # timezone truly absent
    assert cm._missing_defaults(shipped, live) == ["site.timezone"]


def test_ignore_lists_respected():
    shipped = {
        "site.managed": False,          # operational state — ignored path
        "consultants": [],              # per-clinic data — ignored top-level
        "site.language": "es",          # genuinely missing
    }
    live = {"site.name": "Acme"}
    assert cm._missing_defaults(shipped, live) == ["site.language"]


def test_exact_leaf_match_is_present():
    shipped = {"site.language": "es"}
    live = {"site.language": "en"}       # present (value differs, not our concern)
    assert cm._missing_defaults(shipped, live) == []
