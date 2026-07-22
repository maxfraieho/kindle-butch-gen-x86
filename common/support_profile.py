"""Read-only Appwrite profile lookup for the support system (TASK-49).

Architectural split (Q): Appwrite = registration/referrals only; ALL AI
generation stays on the phone. The phone therefore only ever READS here,
with a hard timeout, and every failure path answers with the safe
defaults - the banner counts as ENABLED and book generation is NEVER
blocked by the external service being down.

Credentials deliberately live OUTSIDE git:
  - key:  env KBG_APPWRITE_KEY, or first line of ~/.kbg_appwrite_key
  - who:  `appwrite` section of support_config.json (endpoint, project,
          telegram_id of this installation's owner)
"""
import json
import os

import requests

from common.support_banner import CONFIG_PATH

_TIMEOUT_S = 3
_SAFE_DEFAULTS = {"banner_disabled": False, "priority_tier": 0}


_TERMUX_HOME = os.environ.get("TERMUX_HOME", "/data/data/com.termux/files/home")


def _read_key():
    key = os.environ.get("KBG_APPWRITE_KEY", "").strip()
    if key:
        return key
    # Two candidates because $HOME differs between Termux and the proot
    # container (/root) while the Termux home stays bind-mounted at its
    # absolute path - the pipeline INSIDE the container must still find
    # the key (found live: entitlement fail-closed deactivated the cast
    # registry during in-container translation).
    for path in (os.path.expanduser("~/.kbg_appwrite_key"),
                 os.path.join(_TERMUX_HOME, ".kbg_appwrite_key")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            continue
    return ""


def fetch_profile():
    """Return {'banner_disabled': bool, 'priority_tier': int}.

    ANY problem - missing config/key, network error, timeout, HTTP error,
    unexpected payload, user not registered - returns the safe defaults.
    A wrong answer here may show one extra banner; it must never break or
    delay a build.
    """
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            aw = (json.load(f) or {}).get("appwrite") or {}
        endpoint = aw.get("endpoint", "").rstrip("/")
        project = aw.get("project", "")
        tg_id = str(aw.get("telegram_id", "")).strip()
        key = _read_key()
        if not (endpoint and project and tg_id and key):
            return dict(_SAFE_DEFAULTS)

        r = requests.get(
            f"{endpoint}/databases/{aw.get('database', 'kbg-support')}"
            f"/collections/{aw.get('collection', 'users')}/documents",
            headers={"X-Appwrite-Project": project, "X-Appwrite-Key": key},
            params={"queries[]": json.dumps(
                {"method": "equal", "attribute": "telegram_id",
                 "values": [tg_id]})},
            timeout=_TIMEOUT_S,
        )
        r.raise_for_status()
        docs = r.json().get("documents", [])
        if not docs:
            return dict(_SAFE_DEFAULTS)
        doc = docs[0]
        return {
            "banner_disabled": bool(doc.get("banner_disabled")),
            "priority_tier": int(doc.get("priority_tier") or 0),
        }
    except Exception:
        return dict(_SAFE_DEFAULTS)


def remote_banner_disabled():
    return fetch_profile()["banner_disabled"]


def get_priority_tier():
    return fetch_profile()["priority_tier"]


# --- Premium entitlements (TASK-53) -------------------------------------
# Opposite fallback direction from the banner: paid features FAIL CLOSED
# (Appwrite unreachable -> feature unavailable; generation itself is never
# blocked - the opt-in toggle just cannot activate). A 7-day grace cache
# of the last successful read keeps a temporary network loss from locking
# out someone who already donated.
import time

_ENTITLEMENT_CACHE = (os.path.join(_TERMUX_HOME, ".vydra_entitlements.json")
                      if os.path.isdir(_TERMUX_HOME)
                      else os.path.expanduser("~/.vydra_entitlements.json"))
_GRACE_SECONDS = 7 * 24 * 3600


def _fetch_entitlements_remote():
    """Return list of entitlement strings, or None on ANY failure
    (None != empty list: None means 'could not read')."""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            aw = (json.load(f) or {}).get("appwrite") or {}
        endpoint = aw.get("endpoint", "").rstrip("/")
        project = aw.get("project", "")
        tg_id = str(aw.get("telegram_id", "")).strip()
        key = _read_key()
        if not (endpoint and project and tg_id and key):
            return None
        r = requests.get(
            f"{endpoint}/databases/{aw.get('database', 'kbg-support')}"
            f"/collections/{aw.get('collection', 'users')}/documents",
            headers={"X-Appwrite-Project": project, "X-Appwrite-Key": key},
            params={"queries[]": json.dumps(
                {"method": "equal", "attribute": "telegram_id",
                 "values": [tg_id]})},
            timeout=_TIMEOUT_S,
        )
        r.raise_for_status()
        docs = r.json().get("documents", [])
        raw = (docs[0].get("entitlements") or "") if docs else ""
        return [e for e in raw.split(",") if e]
    except Exception:
        return None


def get_entitlements():
    """List of active entitlements - live, or fresh (<7d) grace cache on
    failure, else empty. Display-oriented twin of is_entitled()."""
    ents = _fetch_entitlements_remote()
    if ents is not None:
        try:
            with open(_ENTITLEMENT_CACHE, "w", encoding="utf-8") as f:
                json.dump({"ts": time.time(), "entitlements": ents}, f)
        except OSError:
            pass
        return ents
    try:
        with open(_ENTITLEMENT_CACHE, "r", encoding="utf-8") as f:
            cached = json.load(f)
        if time.time() - float(cached.get("ts", 0)) <= _GRACE_SECONDS:
            return cached.get("entitlements") or []
    except Exception:
        pass
    return []


def is_entitled(feature):
    """True iff the profile has `feature` - live, or from a fresh (<7d)
    grace cache when the live read fails. Everything else is False."""
    ents = _fetch_entitlements_remote()
    if ents is not None:
        try:
            with open(_ENTITLEMENT_CACHE, "w", encoding="utf-8") as f:
                json.dump({"ts": time.time(), "entitlements": ents}, f)
        except OSError:
            pass
        return feature in ents
    try:
        with open(_ENTITLEMENT_CACHE, "r", encoding="utf-8") as f:
            cached = json.load(f)
        if time.time() - float(cached.get("ts", 0)) <= _GRACE_SECONDS:
            return feature in (cached.get("entitlements") or [])
    except Exception:
        pass
    return False


# --- Multi-device count (TASK-73) ----------------------------------------
# Display-only - the actual free-tier limit is enforced server-side (in
# tg-support-bot's _heartbeat(), which stamps over_limit on any device
# beyond MAX_FREE_DEVICES). This just tells THIS device's dashboard how
# many devices its own account currently has, so the UI can show a
# "you're at N/3, unlock more" note without needing to guess.
MAX_FREE_DEVICES = 3


def get_device_status():
    """Return {'count': int, 'limit': int, 'over_limit': bool}. Same
    fail-safe contract as the rest of this module - any problem reads as
    'nothing to report' (count=0), never blocks the dashboard."""
    safe = {"count": 0, "limit": MAX_FREE_DEVICES, "over_limit": False}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            aw = (json.load(f) or {}).get("appwrite") or {}
        endpoint = aw.get("endpoint", "").rstrip("/")
        project = aw.get("project", "")
        tg_id = str(aw.get("telegram_id", "")).strip()
        key = _read_key()
        if not (endpoint and project and tg_id and key):
            return safe
        r = requests.get(
            f"{endpoint}/databases/{aw.get('database', 'kbg-support')}"
            f"/collections/device_sessions/documents",
            headers={"X-Appwrite-Project": project, "X-Appwrite-Key": key},
            params={"queries[]": json.dumps(
                {"method": "equal", "attribute": "telegram_id",
                 "values": [tg_id]})},
            timeout=_TIMEOUT_S,
        )
        r.raise_for_status()
        docs = r.json().get("documents", [])
        return {
            "count": len(docs),
            "limit": MAX_FREE_DEVICES,
            "over_limit": any(d.get("over_limit") for d in docs),
        }
    except Exception:
        return safe
