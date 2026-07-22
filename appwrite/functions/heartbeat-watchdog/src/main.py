"""Vydra (🦦) conversion heartbeat watchdog — Appwrite Function.

**Deploy this function as SCHEDULE-TRIGGER ONLY, with no HTTP execute
permission granted to anyone (including "any").** Unlike tg-support-bot,
this function does not authenticate its invocation - it relies entirely
on Appwrite's own trigger-permission model to ensure it only ever runs
on the configured cron schedule, never from an arbitrary HTTP caller.
Do not add an HTTP-callable path to this function without adding real
request authentication first.

Purpose: devices periodically heartbeat their active conversion via
tg-support-bot's /heartbeat path (see that function's `_heartbeat()`),
writing `last_heartbeat_ts` + `active_book_slug` on a `device_sessions`
document. Termux/Android can silently kill the whole phone-side process
tree (confirmed live 2026-07-19 - twice in one session) with zero signal
reaching the user; auto-resume-on-restart handles recovery once the user
reopens Termux, but nothing told them to. This function scans for
device sessions whose heartbeat has gone stale while a book was still
marked active, and sends a Telegram nudge to reopen Termux.

TASK-72 (multi-device, 2026-07-19): a telegram_id can have several
devices (phone + tablet) each with their own device_sessions row -
notifications are sent as one per-account DIGEST covering every device's
current state, not one message per stale device, following the same
pattern established sync tools (Syncthing/Resilio) use for multi-node
status: name the specific node that's down, but also show the rest of
the fleet is fine, so the user isn't left guessing whether everything
broke or just one device.

Recommended schedule: every 5 minutes (`*/5 * * * *`).
"""
import os
from datetime import datetime, timezone

import requests
from appwrite.client import Client
from appwrite.services.databases import Databases
from appwrite.query import Query

DB_ID = "kbg-support"
COLL_ID = "users"
DEVICE_COLL_ID = "device_sessions"
NOTIFY_FUNCTION_ID = "kbg-tg-support-bot"

# 5 min (Q's explicit call, 2026-07-19 - 15min felt too slow): still well
# above the phone's own per-page heartbeat cadence (roughly one heartbeat
# every 20-40s during active translation) and above the 5-minute schedule
# interval itself, so a real stall is caught on close to the first check
# after it happens without false-firing on ordinary per-page variance.
STALE_SECONDS = 5 * 60

# Q's explicit call, 2026-07-19: 30min felt too long, matches STALE_SECONDS
# now - re-nudge on essentially every schedule tick while still down,
# rather than going quiet for half an hour.
REALERT_SECONDS = 5 * 60


def _tg_send(endpoint, project, api_key, watchdog_secret, chat_id, text):
    """Routed through tg-support-bot's own _send_notification handler
    instead of holding a TELEGRAM_BOT_TOKEN here directly - one less
    place that sensitive value lives. Uses the Functions execution API,
    the same mechanism (and the SAME account API key this function's own
    x-appwrite-key already grants) used to test this path manually."""
    try:
        requests.post(
            f"{endpoint}/functions/{NOTIFY_FUNCTION_ID}/executions",
            headers={"X-Appwrite-Project": project, "X-Appwrite-Key": api_key,
                     "Content-Type": "application/json"},
            json={
                "async": True,
                "headers": {"x-vydra-watchdog-secret": watchdog_secret},
                "body": __import__("json").dumps({"chat_id": chat_id, "text": text}),
            },
            timeout=10,
        )
    except requests.RequestException:
        pass  # best-effort; a missed nudge isn't worth failing the run over


def _resume_note(stage):
    """Every registered process type auto-resumes on Termux restart
    (bin/resume_active_conversion.py), but what that means for the USER
    differs: some genuinely finish unattended, others still need a human
    step afterward. Keep this in sync with every send_heartbeat(...,
    stage=...) call across the codebase."""
    stage_l = (stage or "").lower()
    if "агент" in stage_l:
        # source="gemma_agent" edits always land pending, never
        # auto-applied - resuming the scan doesn't apply anything by itself.
        return "відкрийте вкладку «Агент» і натисніть запуск ще раз"
    if "сканування персонажів" in stage_l:
        return ("сканування продовжиться автоматично; перевірте нових "
                "персонажів у вкладці «Cast & Context» після завершення")
    # переклад / переклад книги / озвучення - all fully unattended resumes.
    return "продовжиться з того ж місця автоматично"


def _build_digest(sessions, stale_ids):
    """One message per account covering every device's current state,
    not one message per stale device - the whole point of the digest
    pattern is answering "is this just one device, or is everything
    down?" without the user having to guess or open the app."""
    stale_blocks = []
    status_lines = []
    for s in sessions:
        alias = s.get("device_alias") or "пристрій"
        is_stale = s["$id"] in stale_ids
        book = s.get("active_book_slug") or "книгу"
        progress = s.get("active_book_progress") or ""
        stage = s.get("active_book_stage") or "переклад"
        progress_txt = f" ({progress})" if progress else ""
        icon = "⏸️" if is_stale else "▶️"
        status = "зупинився(-лася)" if is_stale else "працює"
        status_lines.append(f"{icon} {alias}: {status}{progress_txt} — «{book}», етап: {stage}")
        if is_stale:
            stale_blocks.append(
                f"⏸️ Схоже, {stage} книги «{book}»{progress_txt} на пристрої "
                f"«{alias}» зупинився(-лася) — Termux на ньому міг закритися сам. "
                f"Відкрийте застосунок Termux на цьому пристрої ще раз, "
                f"{_resume_note(stage)}."
            )
    return ("\n\n".join(stale_blocks)
            + "\n\n📊 Поточний стан ваших пристроїв:\n"
            + "\n".join(status_lines))


def main(context):
    res = context.res
    watchdog_secret = os.environ.get("WATCHDOG_SECRET", "")
    if not watchdog_secret:
        context.log("WATCHDOG_SECRET not configured - nothing to do.")
        return res.json({"ok": False, "error": "watchdog secret not configured"})
    endpoint = os.environ.get("APPWRITE_FUNCTION_API_ENDPOINT", "https://fra.cloud.appwrite.io/v1")
    project = os.environ["APPWRITE_FUNCTION_PROJECT_ID"]
    api_key = context.req.headers.get("x-appwrite-key", "")

    client = Client().set_endpoint(endpoint).set_project(project).set_key(api_key)
    db = Databases(client)

    now = int(datetime.now(timezone.utc).timestamp())
    all_sessions = db.list_documents(DB_ID, DEVICE_COLL_ID, queries=[
        Query.is_not_null("active_book_slug"),
        Query.limit(100),
    ]).get("documents", [])
    # TASK-73: a device beyond the free per-account limit is marked
    # over_limit at creation (see tg-support-bot's _heartbeat()) and is
    # excluded from watchdog coverage entirely - no stall detection, no
    # digest mention. The dashboard (/api/support/profile) is where the
    # user learns about it instead - a spammy per-tick Telegram nudge
    # about a paywall isn't the right channel for that message.
    active_sessions = [s for s in all_sessions if not s.get("over_limit")]

    by_user = {}
    for s in active_sessions:
        by_user.setdefault(s.get("telegram_id", ""), []).append(s)

    nudged = 0
    for tg_id, sessions in by_user.items():
        if not tg_id:
            continue
        stale_ids = set()
        triggering_ids = set()
        for s in sessions:
            last_hb = int(s.get("last_heartbeat_ts") or 0)
            if last_hb == 0 or now - last_hb < STALE_SECONDS:
                continue
            stale_ids.add(s["$id"])
            last_alert = int(s.get("last_stall_alert_ts") or 0)
            if now - last_alert >= REALERT_SECONDS:
                triggering_ids.add(s["$id"])
        if not triggering_ids:
            continue  # nothing new to report for this account this tick

        user_docs = db.list_documents(DB_ID, COLL_ID, queries=[
            Query.equal("telegram_id", tg_id), Query.limit(1),
        ]).get("documents", [])
        if not user_docs:
            continue
        user = user_docs[0]
        # TASK-70: explicit user opt-out (/pause, /resume in tg-support-bot),
        # account-level - applies to every device under this telegram_id.
        if user.get("watchdog_paused"):
            continue

        text = _build_digest(sessions, stale_ids)
        _tg_send(endpoint, project, api_key, watchdog_secret, int(tg_id), text)
        for session_id in triggering_ids:
            db.update_document(DB_ID, DEVICE_COLL_ID, session_id,
                               data={"last_stall_alert_ts": now})
        nudged += 1

    context.log(f"Checked {len(active_sessions)} active device session(s) "
               f"across {len(by_user)} account(s), nudged {nudged}.")
    return res.json({"ok": True, "checked": len(active_sessions), "nudged": nudged})
