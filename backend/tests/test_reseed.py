"""Unit tests for core.reseed_client — the per-instance nuke-and-reseed the
client detail modal drives (seed --reset). Pure command-construction + result
handling: run_ssh is monkeypatched, so nothing here touches SSH or a box.
Self-contained like test_model_catalog (no shared harness yet):
`pytest backend/tests/test_reseed.py`."""
from backend import core


def _capture(monkeypatch, ret=(True, "Seeded (...)\n===RESEED_OK===")):
    seen = {}

    def fake_run_ssh(target, cmd, timeout=0):
        seen.update(target=target, cmd=cmd, timeout=timeout)
        return ret

    monkeypatch.setattr(core, "run_ssh", fake_run_ssh)
    return seen


def test_runs_seed_demo_pinned_and_restarts(monkeypatch):
    seen = _capture(monkeypatch)
    r = core.reseed_client("deploy@host", "~/dental-clinic-agent")
    assert r["ok"] is True
    cmd = seen["cmd"]
    # the app's own seed module — --demo wipes AND regenerates demo clients/
    # chats/bookings (not --reset, which leaves the demo box bare)
    assert "python -m backend.db.seed --demo" in cmd
    assert "--reset" not in cmd
    # project name pinned per-client (shared-VPS incident — see _project_name)
    assert "-p dental-clinic-agent" in cmd
    # container restarted so the in-memory live-tail clears too
    assert "restart app" in cmd


def test_missing_ssh_target_or_dir_rejected(monkeypatch):
    _capture(monkeypatch)
    assert core.reseed_client("", "~/d")["ok"] is False
    assert core.reseed_client("deploy@host", "")["ok"] is False


def test_ssh_failure_surfaces_as_error(monkeypatch):
    _capture(monkeypatch, ret=(False, "boom"))
    r = core.reseed_client("deploy@host", "~/d")
    assert r["ok"] is False and r["error"] == "boom"


def test_missing_sentinel_treated_as_failure(monkeypatch):
    # run_ssh returned success but the command didn't reach the end marker —
    # e.g. seed crashed mid-run. Must be reported as a failure, not success.
    _capture(monkeypatch, ret=(True, "Traceback ... KeyError"))
    r = core.reseed_client("deploy@host", "~/d")
    assert r["ok"] is False
