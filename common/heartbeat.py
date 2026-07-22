"""Conversion/agent heartbeat (TASK-68 follow-up, 2026-07-19).

Best-effort, fire-and-forget notice of "this long-running phone-side
process is still alive and at what point" so a stalled/killed Termux can
be detected externally and the user nudged via Telegram - nothing left
running ON the phone can notify them once Termux itself is dead, so the
signal has to reach an always-on service (Appwrite) instead.

Deliberately the ONE write path the phone takes against Appwrite - see
common/support_profile.py's docstring: the phone is read-only there by
architectural choice (Q), so external-service downtime can never block
generation. This module's contract preserves that: every failure here
is silently swallowed and NEVER allowed to slow or interrupt the caller.

Used by: translate_manga.py (per-page), bin/agent_editor.py (per-case).
Not yet wired into: translate_stage.py / audio_stage.py /
run_conversion_batches.py (the novel/epub pipeline's separate per-stage
scripts) - left for a follow-up pass, see project memory.

TASK-57 (2026-07-19): send_heartbeat() alone left active_book_slug set
in Appwrite forever once the last page/case was processed - nothing ever
told the DB "this book is done". heartbeat-watchdog only checks
last_heartbeat_ts staleness, not completion, so every finished book
eventually tripped a false "stopped" nudge (confirmed live: a 194/194
book, twice in a row - REALERT_SECONDS re-fires every schedule tick
since the state never clears). clear_heartbeat() is the fix: call it
once at the true success point of each pipeline, after the loop that
calls send_heartbeat() (even if that loop made zero calls, e.g. a
resumed run where every page was already done).

Requires (not yet provisioned by deploy.sh - manual setup):
  - global_settings.json's "appwrite" section needs a new
    "heartbeat_secret" field (matching HEARTBEAT_SECRET on the
    tg-support-bot Appwrite Function's env).
  - A separate "heartbeat-watchdog" Appwrite Function
    (appwrite/functions/heartbeat-watchdog/) deployed as
    SCHEDULE-TRIGGER ONLY (e.g. every 5 min), no HTTP execute
    permission granted.
  - New attributes on the "users" collection: watchdog_paused (bool,
    TASK-70's /pause - set only by tg-support-bot, never by this
    module) - account-level, applies to every device.

TASK-72 (multi-device, 2026-07-19): a single telegram_id can now have
several devices heartbeating independently (phone + tablet) - per-device
state (active_book_slug/progress/stage/last_heartbeat_ts) moved out of
the singular "users" document into a new "device_sessions" collection,
keyed by device_id (see common/device_identity.py), to avoid two devices
overwriting each other's state on every heartbeat. Entitlements and
watchdog_paused stay account-level on "users" - unaffected.
"""
import json
import os

import requests

from common.device_identity import get_or_create_device_id, get_device_alias

_CFG = {"initialized": False, "url": None, "project": None, "secret": None, "tg_id": None}


def _init():
    if _CFG["initialized"]:
        return
    _CFG["initialized"] = True
    try:
        from common.support_banner import CONFIG_PATH
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            aw = (json.load(f) or {}).get("appwrite") or {}
        endpoint = aw.get("endpoint", "").rstrip("/")
        project = aw.get("project", "")
        tg_id = str(aw.get("telegram_id", "")).strip()
        secret = (os.environ.get("KBG_HEARTBEAT_SECRET", "").strip()
                  or str(aw.get("heartbeat_secret", "")).strip())
        function_id = aw.get("heartbeat_function_id", "kbg-tg-support-bot")
        if not (endpoint and project and tg_id and secret):
            return
        _CFG.update({
            "url": f"{endpoint}/functions/{function_id}/executions",
            "project": project,
            "secret": secret,
            "tg_id": tg_id,
        })
    except Exception:
        pass


def _post(book_slug, progress_label, stage):
    if not _CFG["initialized"]:
        _init()
    if not _CFG.get("url"):
        return
    try:
        requests.post(
            _CFG["url"],
            headers={"X-Appwrite-Project": _CFG["project"]},
            json={
                "async": True,
                "headers": {"x-vydra-heartbeat-secret": _CFG["secret"]},
                "body": json.dumps({
                    "telegram_id": _CFG["tg_id"],
                    "device_id": get_or_create_device_id(),
                    "device_alias": get_device_alias(),
                    "book_slug": book_slug,
                    "progress": progress_label,
                    "stage": stage,
                }),
            },
            timeout=3,
        )
    except Exception:
        pass  # heartbeat is best-effort - never let it slow the caller


def send_heartbeat(slug, progress_label, stage="переклад"):
    """progress_label: short human string, e.g. '42/194' or 'сторінка 7 з 12'.
    stage: what kind of work this is, shown verbatim in the stall alert so
    the user knows what to expect on restart - e.g. 'переклад',
    'агент-редактор', 'озвучення'. Keep it a short Ukrainian noun phrase,
    it's interpolated directly into the Telegram message."""
    _post(slug, progress_label, stage)


def clear_heartbeat():
    """Counterpart to send_heartbeat(): tells Appwrite this book's run is
    over so heartbeat-watchdog stops waiting for further heartbeats on it
    (empty book_slug -> tg-support-bot's _heartbeat() stores it as None,
    which drops the user out of the watchdog's active-book query). Call
    exactly once, at the pipeline's genuine success point - never from an
    except/failure path, or a real stall would go undetected (TASK-57
    test scenario: real crash must still alert)."""
    _post("", "", "")
