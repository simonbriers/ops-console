"""Finding 5.1 — the uptime band lives once (history.uptime_band) and is folded
into the client's overall status (core.apply_uptime_to_status) so the dashboard
status dot and the detail-modal uptime tile can no longer disagree."""
from backend import history, core


def test_uptime_band_thresholds():
    assert history.uptime_band(None) is None
    assert history.uptime_band(100.0) == "ok"
    assert history.uptime_band(99.0) == "ok"      # 99 is the ok floor
    assert history.uptime_band(98.9) == "warn"
    assert history.uptime_band(95.0) == "warn"    # 95 is the warn floor
    assert history.uptime_band(94.9) == "down"


def test_apply_uptime_upgrades_ok_to_warning():
    for band in ("warn", "down"):
        r = {"status": "ok"}
        core.apply_uptime_to_status(r, {"uptime_band": band})
        assert r["status"] == "warning", band


def test_apply_uptime_never_downgrades_or_touches_healthy():
    r = {"status": "down"}                                  # a real down stays down
    core.apply_uptime_to_status(r, {"uptime_band": "down"})
    assert r["status"] == "down"

    r = {"status": "ok"}                                    # good uptime → unchanged
    core.apply_uptime_to_status(r, {"uptime_band": "ok"})
    assert r["status"] == "ok"

    r = {"status": "ok"}                                    # no uptime data → unchanged
    core.apply_uptime_to_status(r, {})
    assert r["status"] == "ok"
