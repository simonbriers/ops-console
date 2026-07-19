"""site_config.yaml validation — the ops-console port of the dental repo's
deploy/validate_site_config.py rules (see MULTI_CLINIC_ONBOARDING.md for
the two incidents that created them). Pure functions over a parsed dict +
the raw YAML text, so this runs identically on a generated starter config,
a bundle-generated config, or a live instance's /data/site_config.yaml
pulled over SSH.

If these rules and the dental repo's script ever drift, the dental repo's
`backend/config.py` boot guards are the ground truth — these exist to
catch problems BEFORE a crash-looping container does.
"""
from __future__ import annotations

import re
from typing import Any

try:
    from zoneinfo import available_timezones
    _TZ = available_timezones()
except Exception:  # pragma: no cover — zoneinfo data missing on exotic hosts
    _TZ = None

# Mirrors the dental repo's EU_VOICE_PROVIDERS boot guard (observed in its
# startup log: "allowed: ['gladia', 'local', 'mistral', 'piper']"). The
# guard there checks configured providers UNCONDITIONALLY — even with
# voice.enabled false — so a later admin toggle can't bypass it. When
# site.eu_medical_data_protection is false, the guard is skipped there;
# we report a warning instead of an error in that case.
EU_VOICE_PROVIDERS = {"gladia", "local", "mistral", "piper"}

_HHMM_RE = re.compile(r"^\d{2}:\d{2}$")
_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def validate_site_config(cfg: dict[str, Any]) -> dict[str, list[str]]:
    """Returns {"errors": [...], "warnings": [...]} — errors are things the
    dental backend will crash or misbehave on; warnings are near-certain
    operator mistakes that still boot."""
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(cfg, dict):
        return {"errors": ["config is not a YAML mapping at the top level"], "warnings": []}

    site = cfg.get("site")
    if not isinstance(site, dict):
        return {"errors": ["missing top-level `site:` block (note: the key is `site`, not `clinic`)"],
                "warnings": []}

    # --- identity ---------------------------------------------------------
    name = site.get("name") or ""
    if not str(name).strip():
        errors.append("site.name is empty")
    elif "placeholder" in str(name).lower() or "starter" in str(name).lower():
        warnings.append(f"site.name looks like starter content: {name!r}")

    lang = str(site.get("language", "")).lower()
    if lang not in ("es", "en"):
        errors.append(f"site.language must be 'es' or 'en', got {site.get('language')!r}")

    tz = site.get("timezone")
    if not tz:
        errors.append("site.timezone is missing")
    elif _TZ is not None and tz not in _TZ:
        errors.append(f"site.timezone {tz!r} is not a valid IANA timezone")

    color = site.get("brand_color")
    if color is not None and not _HEX_RE.match(str(color)):
        errors.append(f"site.brand_color must be '#rrggbb', got {color!r}")

    # --- hours: the sexagesimal-YAML landmine -----------------------------
    # YAML 1.1 parses an unquoted two-digit `10:00` as the integer 600
    # (sexagesimal); `09:00` accidentally survives as a string because the
    # leading zero breaks the resolver regex. So: any int in an hours list
    # means someone wrote unquoted times. (MULTI_CLINIC_ONBOARDING.md #2.)
    hours = site.get("hours")
    if isinstance(hours, dict):
        for day, values in hours.items():
            if str(day) not in _DAYS:
                warnings.append(f"site.hours has unknown day key {day!r}")
            if not isinstance(values, list):
                errors.append(f"site.hours.{day} must be a list of 'HH:MM' strings")
                continue
            for v in values:
                if isinstance(v, int):
                    errors.append(
                        f"site.hours.{day} contains {v!r} — an UNQUOTED time "
                        "(YAML parses `10:00` as the integer 600). Quote every "
                        "hour: '10:00', no exceptions.")
                elif not isinstance(v, str) or not _HHMM_RE.match(v):
                    errors.append(f"site.hours.{day} value {v!r} is not 'HH:MM'")
    elif hours is not None:
        errors.append("site.hours must be a mapping of weekday -> [open, close]")

    # --- numeric sanity ---------------------------------------------------
    for key, lo in (("booking_horizon_days", 1), ("scheduling_granularity_minutes", 5),
                    ("min_lead_minutes", 0), ("max_upcoming_per_client", 1)):
        v = site.get(key)
        if v is not None and (not isinstance(v, int) or v < lo):
            errors.append(f"site.{key} must be an integer >= {lo}, got {v!r}")

    # --- services / consultants ------------------------------------------
    services = cfg.get("services")
    if not isinstance(services, list) or not services:
        errors.append("`services:` is missing or empty — the bot has nothing to book")
    else:
        for i, svc in enumerate(services):
            if not isinstance(svc, dict) or not str(svc.get("name", "")).strip():
                errors.append(f"services[{i}] has no name")
                continue
            dur = svc.get("duration_min")
            if not isinstance(dur, int) or dur <= 0:
                errors.append(f"service {svc.get('name')!r}: duration_min must be a positive integer")

    consultants = cfg.get("consultants")
    if not isinstance(consultants, list) or not consultants:
        errors.append("`consultants:` is missing or empty — check_slots will never return a slot")
    else:
        for i, c in enumerate(consultants):
            if not isinstance(c, dict) or not str(c.get("name", "")).strip():
                errors.append(f"consultants[{i}] has no name")

    # --- voice: the EU-provider boot guard --------------------------------
    voice = cfg.get("voice")
    if isinstance(voice, dict):
        eu_relaxed = site.get("eu_medical_data_protection") is False
        for role in ("stt", "tts", "llm"):
            block = voice.get(role)
            provider = (block or {}).get("provider") if isinstance(block, dict) else None
            if provider and provider not in EU_VOICE_PROVIDERS:
                msg = (f"voice.{role}.provider {provider!r} is not an EU provider "
                       f"(allowed: {sorted(EU_VOICE_PROVIDERS)}). The backend checks this "
                       "at boot even with voice.enabled false")
                if eu_relaxed:
                    warnings.append(msg + " — currently only a warning because "
                                    "site.eu_medical_data_protection is false.")
                else:
                    errors.append(msg + " and will REFUSE to boot in production.")

    return {"errors": errors, "warnings": warnings}


def validate_yaml_text(text: str) -> dict[str, Any]:
    """Parse + validate raw YAML text. Returns
    {"ok": bool, "errors": [...], "warnings": [...]}; a parse failure is
    itself an error rather than an exception."""
    import yaml  # deferred so importing this module never requires pyyaml
    try:
        cfg = yaml.safe_load(text)
    except yaml.YAMLError as e:
        return {"ok": False, "errors": [f"YAML does not parse: {e}"], "warnings": []}
    result = validate_site_config(cfg or {})
    return {"ok": not result["errors"], **result}
