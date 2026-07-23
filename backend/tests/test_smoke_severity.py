"""Finding 5.2 — a smoke check's warn-vs-critical severity is declared once, on
the row (smoke._row), instead of being re-derived by string-matching the
"backup_timer" check name in five places."""
from backend import smoke


def test_row_defaults_to_critical():
    r = smoke._row("chat_roundtrip", True, "ok")
    assert r["severity"] == "critical"


def test_row_can_be_warn_level():
    r = smoke._row("backup_timer", False, "inactive", severity="warn")
    assert r["severity"] == "warn"
    assert r["check"] == "backup_timer" and r["ok"] is False


def test_suite_pass_ignores_warn_rows():
    # the verdict rule callers now use: pass = every CRITICAL row ok; warn rows
    # (a failing backup_timer) don't fail the suite.
    rows = [smoke._row("a", True, ""),
            smoke._row("backup_timer", False, "", severity="warn")]
    passed = all(r["ok"] for r in rows if r.get("severity", "critical") != "warn")
    assert passed is True
