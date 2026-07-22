"""Vydra (🦦) support-system Telegram bot — Appwrite Function (TASK-49).

HTTP-triggered by a Telegram webhook. Commands:
  /start [ref_code]  — register, generate personal referral code; with a
                       valid deep-link code, records referred_by and
                       bumps the referrer's priority_tier by 1.
  /referral          — personal code + how many people joined with it.
  /no_support_banner — one-step opt-out, instant confirmation.

Security: every request must carry Telegram's
X-Telegram-Bot-Api-Secret-Token matching TG_WEBHOOK_SECRET (set when the
webhook is registered) — anything else gets 401 and no processing.

Architectural split (Q, 2026-07-17): Appwrite = registration/referrals/
site only. All AI generation stays self-hosted on the phone; the phone
reads this database read-only and NEVER blocks on it.
"""
import json
import os
import secrets

import requests
from appwrite.client import Client
from appwrite.services.databases import Databases
from appwrite.query import Query
from appwrite.id import ID

DB_ID = "kbg-support"
COLL_ID = "users"

# TASK-73: free devices per account before cast_registry is required.
MAX_FREE_DEVICES = 3


def _tg_send(token, chat_id, text, keyboard=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json=payload, timeout=10,
        )
    except requests.RequestException:
        pass  # Telegram hiccup must not fail the webhook (it would retry forever)


# Main inline menu shown after /start and /menu. Donation/payment entries
# are deliberate STUBS for now (Q, 2026-07-17) - a payment/donate service
# will be integrated later; Track A is a real URL already (official fund).
def _main_keyboard():
    return [
        [{"text": "📲 Встановити Android (Termux)", "callback_data": "install"},
         {"text": "💻 Встановити x86 (WSL2 / CUDA)", "callback_data": "install_x86"}],
        [{"text": "👥 Реферальний код", "callback_data": "referral"},
         {"text": "🔕 Вимкнути банер", "callback_data": "nobanner"}],
        [{"text": "⏸️ Призупинити сповіщення", "callback_data": "pause"},
         {"text": "▶️ Відновити сповіщення", "callback_data": "resume"}],
        [{"text": "🇺🇦 Донат фонду (офіційно)", "url": "https://savelife.in.ua/donate/"}],
        [{"text": "☕ Підтримати розробника", "callback_data": "donate_dev"},
         {"text": "💳 Розширені можливості / оплата", "callback_data": "premium"}],
    ]


def _get_user(db, tg_id):
    res = db.list_documents(DB_ID, COLL_ID,
                            queries=[Query.equal("telegram_id", str(tg_id))])
    docs = res.get("documents", [])
    return docs[0] if docs else None


DEVICE_COLL_ID = "device_sessions"


def _get_device_session(db, device_id):
    res = db.list_documents(DB_ID, DEVICE_COLL_ID,
                            queries=[Query.equal("device_id", device_id)])
    docs = res.get("documents", [])
    return docs[0] if docs else None


def _heartbeat(context):
    """Best-effort conversion heartbeat (TASK-68 follow-up; TASK-72
    multi-device). The phone POSTs here periodically while a book is
    converting, carrying its own telegram_id + device_id + progress.
    Written with its own dedicated secret header (HEARTBEAT_SECRET) -
    deliberately NOT reusing TG_WEBHOOK_SECRET or the phone's read-only
    entitlement key, so a leaked heartbeat secret can't be used to forge
    Telegram webhook calls or vice versa.

    Deliberately narrow: this is the ONE write path the phone is allowed
    against Appwrite (see common/support_profile.py's docstring - the
    phone is read-only otherwise, by Q's explicit architectural choice).
    Any failure here must never affect the phone's own translation loop,
    which is why the phone-side caller (translate_manga.py) treats this
    entire call as fire-and-forget with a short timeout.

    TASK-72: per-device state lives in "device_sessions" (keyed by
    device_id, unique index), NOT on the singular "users" document -
    two devices under the same telegram_id must never overwrite each
    other's active_book_slug/progress on every heartbeat. entitlements/
    watchdog_paused stay on "users" (account-level, correctly shared).
    """
    req, res = context.req, context.res
    secret = os.environ.get("HEARTBEAT_SECRET", "")
    header = req.headers.get("x-vydra-heartbeat-secret", "")
    if not secret or header != secret:
        return res.json({"ok": False, "error": "unauthorized"}, 401)
    try:
        body = req.body if isinstance(req.body, dict) else json.loads(req.body_raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return res.json({"ok": False, "error": "bad request"}, 400)
    tg_id = str(body.get("telegram_id", "")).strip()
    device_id = str(body.get("device_id", "")).strip()
    device_alias = str(body.get("device_alias", "")).strip()
    book_slug = str(body.get("book_slug", "")).strip()
    progress = str(body.get("progress", "")).strip()
    stage = str(body.get("stage", "")).strip()
    if not tg_id or not device_id:
        # device_id is required (unique index on device_sessions - an
        # empty value from a not-yet-updated phone would collide across
        # every legacy caller). Silent no-op, not an error the phone
        # should see or retry over - matches the pre-existing contract
        # for "nothing to attach this to".
        return res.json({"ok": True})

    client = (Client()
              .set_endpoint(os.environ.get("APPWRITE_FUNCTION_API_ENDPOINT",
                                           "https://fra.cloud.appwrite.io/v1"))
              .set_project(os.environ["APPWRITE_FUNCTION_PROJECT_ID"])
              .set_key(req.headers.get("x-appwrite-key", "")))
    db = Databases(client)
    user = _get_user(db, tg_id)
    if not user:
        # Heartbeat from an unregistered phone - nothing to attach it to.
        # Not an error the phone should ever see or retry over.
        return res.json({"ok": True})

    from datetime import datetime, timezone
    now_ts = int(datetime.now(timezone.utc).timestamp())
    data = {
        "telegram_id": tg_id,
        "device_id": device_id,
        "last_heartbeat_ts": now_ts,
        "active_book_slug": book_slug or None,
        "active_book_progress": progress or None,
        "active_book_stage": stage or None,
    }
    if device_alias:
        data["device_alias"] = device_alias
    session = _get_device_session(db, device_id)
    if session:
        db.update_document(DB_ID, DEVICE_COLL_ID, session["$id"], data=data)
    else:
        # TASK-73: free tier caps distinct devices per account at
        # MAX_FREE_DEVICES; cast_registry (the same single entitlement
        # that unlocks every other premium feature, TASK-56) lifts it.
        # Deliberately does NOT block the heartbeat write itself -
        # generation is fully local/offline and was never gated by
        # Appwrite reachability by design (Q's architecture split), so
        # the only thing a device limit can meaningfully restrict is
        # watchdog crash-notification coverage, not usage. A device over
        # the limit is marked, not refused - the dashboard surfaces it
        # (see /api/support/profile's device_count/device_limit fields).
        existing = db.list_documents(DB_ID, DEVICE_COLL_ID, queries=[
            Query.equal("telegram_id", tg_id), Query.limit(100),
        ])
        entitlements = [e for e in (user.get("entitlements") or "").split(",") if e]
        if len(existing.get("documents", [])) >= MAX_FREE_DEVICES and "cast_registry" not in entitlements:
            data["over_limit"] = True
        db.create_document(DB_ID, DEVICE_COLL_ID, ID.unique(), data=data)
    return res.json({"ok": True})


def _send_notification(context):
    """Trusted send-on-behalf-of, called by the heartbeat-watchdog
    function (own dedicated WATCHDOG_SECRET - never the same secret as
    the phone's HEARTBEAT_SECRET or the Telegram webhook's). Lets the
    watchdog send a Telegram message without ever holding its own copy
    of TELEGRAM_BOT_TOKEN - one less place a sensitive token lives."""
    req, res = context.req, context.res
    secret = os.environ.get("WATCHDOG_SECRET", "")
    header = req.headers.get("x-vydra-watchdog-secret", "")
    if not secret or header != secret:
        return res.json({"ok": False, "error": "unauthorized"}, 401)
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return res.json({"ok": False, "error": "bot token not configured"}, 500)
    try:
        body = req.body if isinstance(req.body, dict) else json.loads(req.body_raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return res.json({"ok": False, "error": "bad request"}, 400)
    chat_id = body.get("chat_id")
    text = str(body.get("text", "")).strip()
    if not chat_id or not text:
        return res.json({"ok": False, "error": "chat_id and text required"}, 400)
    _tg_send(token, chat_id, text)
    return res.json({"ok": True})


def main(context):
    req, res = context.req, context.res

    if req.headers.get("x-vydra-watchdog-secret", ""):
        return _send_notification(context)

    if req.headers.get("x-vydra-heartbeat-secret", ""):
        return _heartbeat(context)

    secret = os.environ.get("TG_WEBHOOK_SECRET", "")
    header = req.headers.get("x-telegram-bot-api-secret-token", "")
    if not secret or header != secret:
        return res.json({"ok": False, "error": "unauthorized"}, 401)

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return res.json({"ok": False, "error": "bot token not configured"}, 500)

    try:
        update = req.body if isinstance(req.body, dict) else json.loads(req.body_raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return res.json({"ok": True})  # not for us; ack so Telegram stops retrying

    cb = update.get("callback_query")
    if cb:
        # Ack immediately so the button stops spinning, then treat the
        # callback as its command twin.
        try:
            requests.post(f"https://api.telegram.org/bot{token}/answerCallbackQuery",
                          json={"callback_query_id": cb["id"]}, timeout=10)
        except requests.RequestException:
            pass
        data = cb.get("data") or ""
        chat_id = ((cb.get("message") or {}).get("chat") or {}).get("id")
        tg_id = (cb.get("from") or {}).get("id")
        text = {"install": "/install", "install_x86": "/install_x86", "referral": "/referral",
                "nobanner": "/no_support_banner", "donate_dev": "/donate_dev",
                "premium": "/premium", "pause": "/pause",
                "resume": "/resume"}.get(data, "")
        if not chat_id or not tg_id or not text:
            return res.json({"ok": True})
    else:
        msg = update.get("message") or update.get("edited_message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        tg_id = (msg.get("from") or {}).get("id")
        text = (msg.get("text") or "").strip()
        if not chat_id or not tg_id or not text.startswith("/"):
            return res.json({"ok": True})

    client = (Client()
              .set_endpoint(os.environ.get("APPWRITE_FUNCTION_API_ENDPOINT",
                                           "https://fra.cloud.appwrite.io/v1"))
              .set_project(os.environ["APPWRITE_FUNCTION_PROJECT_ID"])
              .set_key(req.headers.get("x-appwrite-key", "")))
    db = Databases(client)

    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@")[0].lower()
    arg = arg.strip()

    if cmd == "/start":
        user = _get_user(db, tg_id)
        if user:
            _tg_send(token, chat_id,
                     "Ви вже зареєстровані ✅\n"
                     f"Ваш реферальний код: <code>{user['referral_code']}</code>\n"
                     f"Ваш Telegram ID (для прив'язки пристрою в застосунку): <code>{tg_id}</code>\n"
                     "Оберіть дію кнопками нижче 👇",
                     keyboard=_main_keyboard())
            return res.json({"ok": True})

        code = secrets.token_hex(4)
        referred_by = None
        if arg:
            ref = db.list_documents(DB_ID, COLL_ID,
                                    queries=[Query.equal("referral_code", arg)])
            ref_docs = ref.get("documents", [])
            if ref_docs and str(ref_docs[0]["telegram_id"]) != str(tg_id):
                referred_by = arg
                referrer = ref_docs[0]
                db.update_document(DB_ID, COLL_ID, referrer["$id"], data={
                    "priority_tier": int(referrer.get("priority_tier") or 0) + 1,
                })

        from datetime import datetime, timezone
        db.create_document(DB_ID, COLL_ID, ID.unique(), data={
            "telegram_id": str(tg_id),
            "referral_code": code,
            "referred_by": referred_by,
            "banner_disabled": False,
            "priority_tier": 1 if referred_by else 0,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        bonus = ("\n🎁 Ви прийшли за запрошенням — вам і другу нараховано "
                 "пріоритет у черзі генерації.") if referred_by else ""
        _tg_send(token, chat_id,
                 "🦦 Вітаємо у Vydra — книги, аудіокниги та манґа українською,\n"
                 "все локально, на вашому пристрої.\n\n"
                 "🔒 Ми зберігаємо лише ваш Telegram ID і налаштування — щоб "
                 "керувати чергою і синхронізувати статус. Файли ваших книг "
                 "НІКОЛИ не потрапляють на наші сервери. Політика приватності: "
                 "/privacy. Видалити свої дані будь-коли: /delete_my_data.\n\n"
                 f"Ваш реферальний код: <code>{code}</code>{bonus}\n\n"
                 f"Ваш Telegram ID (для прив'язки пристрою в застосунку): <code>{tg_id}</code>\n\n"
                 "Оберіть дію кнопками нижче 👇",
                 keyboard=_main_keyboard())
        return res.json({"ok": True})

    if cmd == "/privacy":
        _tg_send(token, chat_id,
                 "🔒 <b>Політика приватності Vydra</b>\n\n"
                 "<b>Хто збирає:</b> команда розробників Vydra.\n"
                 "<b>Що збираємо:</b> лише через цього бота — ваш Telegram ID, "
                 "згенерований реферальний код і перелік увімкнених розширених можливостей.\n"
                 "<b>Навіщо:</b> автентифікація вашого застосунку, керування чергою "
                 "генерації та синхронізація налаштувань.\n"
                 "<b>Де:</b> база Appwrite у Франкфурті (ЄС).\n"
                 "<b>Чого НЕ збираємо:</b> ваші книги, документи, переклади й аудіо — "
                 "уся обробка відбувається офлайн на вашому пристрої, ми не маємо до неї доступу.\n"
                 "<b>Не передаємо</b> дані третім сторонам.\n"
                 "<b>Ваші права (GDPR):</b> доступ, виправлення, видалення. "
                 "Видалити все — команда /delete_my_data.")
        return res.json({"ok": True})

    if cmd == "/delete_my_data":
        user = _get_user(db, tg_id)
        if not user:
            _tg_send(token, chat_id, "У нас немає ваших даних. Нічого видаляти ✅")
            return res.json({"ok": True})
        try:
            # TASK-74 (code review, orphaned-records finding): the users
            # document alone isn't the whole footprint since TASK-72 -
            # every linked device has its own device_sessions row
            # (telegram_id, progress, heartbeat history). Deleting only
            # the profile left these behind indefinitely - a real GDPR
            # gap for a command whose entire point is "delete everything".
            sessions = db.list_documents(DB_ID, DEVICE_COLL_ID,
                                         queries=[Query.equal("telegram_id", str(tg_id))])
            for s in sessions.get("documents", []):
                try:
                    db.delete_document(DB_ID, DEVICE_COLL_ID, s["$id"])
                except Exception:
                    pass  # best-effort - the user document delete below is the one that must not silently fail
            db.delete_document(DB_ID, COLL_ID, user["$id"])
            _tg_send(token, chat_id,
                     "✅ Ваш запис повністю видалено з наших серверів "
                     "(ID, реферальний код, статуси, дані всіх привʼязаних пристроїв). "
                     "Дякуємо, що були з нами.\n"
                     "Застосунок на вашому пристрої продовжує працювати локально.")
        except Exception as e:
            _tg_send(token, chat_id, f"Не вдалося видалити зараз: {e}. Напишіть /start і спробуйте ще раз.")
        return res.json({"ok": True})

    if cmd == "/referral":
        user = _get_user(db, tg_id)
        if not user:
            _tg_send(token, chat_id, "Спершу зареєструйтесь: /start")
            return res.json({"ok": True})
        invited = db.list_documents(DB_ID, COLL_ID,
                                    queries=[Query.equal("referred_by",
                                                         user["referral_code"])])
        bot_name = os.environ.get("TELEGRAM_BOT_USERNAME", "")
        link = (f"\nПосилання: https://t.me/{bot_name}?start={user['referral_code']}"
                if bot_name else "")
        _tg_send(token, chat_id,
                 f"Ваш код: <code>{user['referral_code']}</code>{link}\n"
                 f"Запрошено: {invited.get('total', 0)}\n"
                 f"Ваш пріоритет у черзі: {user.get('priority_tier') or 0}")
        return res.json({"ok": True})

    if cmd == "/no_support_banner":
        user = _get_user(db, tg_id)
        if not user:
            _tg_send(token, chat_id, "Спершу зареєструйтесь: /start")
            return res.json({"ok": True})
        db.update_document(DB_ID, COLL_ID, user["$id"],
                           data={"banner_disabled": True})
        _tg_send(token, chat_id, "Готово, більше не показуватимемо ✅")
        return res.json({"ok": True})

    if cmd == "/pause":
        # TASK-70 (Q's ask, 2026-07-19): heartbeat-watchdog can't tell
        # "Q silently stepped away" from "Q's phone silently died" - both
        # look identical (stale last_heartbeat_ts). Give an explicit
        # opt-out instead of guessing: while paused, heartbeat-watchdog
        # skips this user entirely regardless of staleness (see its own
        # main() loop). Does NOT stop the phone from sending heartbeats -
        # this only gates whether the watchdog nudges, so resuming a
        # conversion later still reports fresh progress once /resume'd.
        user = _get_user(db, tg_id)
        if not user:
            _tg_send(token, chat_id, "Спершу зареєструйтесь: /start")
            return res.json({"ok": True})
        db.update_document(DB_ID, COLL_ID, user["$id"],
                           data={"watchdog_paused": True})
        _tg_send(token, chat_id,
                 "⏸️ Сповіщення про \"зупинку\" вимкнено, поки ви не напишете "
                 "/resume. Якщо конверсія й справді зависне цей час, ви про "
                 "це не дізнаєтесь звідси.")
        return res.json({"ok": True})

    if cmd == "/resume":
        user = _get_user(db, tg_id)
        if not user:
            _tg_send(token, chat_id, "Спершу зареєструйтесь: /start")
            return res.json({"ok": True})
        db.update_document(DB_ID, COLL_ID, user["$id"],
                           data={"watchdog_paused": False})
        _tg_send(token, chat_id, "▶️ Сповіщення про зупинку знову увімкнено ✅")
        return res.json({"ok": True})

    if cmd == "/install":
        _tg_send(token, chat_id,
                 "🦦 <b>Встановлення Vydra на ваш Android (Termux)</b>\n\n"
                 "Потрібно: 64-бітний телефон, 6+ GB RAM, 15+ GB вільного місця, "
                 "~30 хв і Wi-Fi (завантажується модель 4.4GB).\n\n"
                 "<b>Крок 1.</b> Встановіть Termux З F-DROID (не з Play Market — "
                 "та версія мертва):\n"
                 "https://f-droid.org/packages/com.termux/\n\n"
                 "<b>Крок 2.</b> ⚠️ Вимкніть оптимізацію батареї для Termux "
                 "(найчастіша причина невдалих встановлень — Android вбиває "
                 "процес у фоні навіть з увімкненим екраном):\n"
                 "Налаштування → Застосунки → Termux → Батарея → "
                 "<b>Без обмежень</b>\n\n"
                 "<b>Крок 3.</b> Встановіть Termux:Boot з того ж F-Droid і "
                 "відкрийте його один раз (це дозволить сервісам стартувати "
                 "після перезавантаження):\n"
                 "https://f-droid.org/packages/com.termux.boot/\n\n"
                 "<b>Крок 4.</b> Відкрийте Termux і вставте одну команду:\n"
                 "<code>bash &lt;(curl -fsSL https://raw.githubusercontent.com/"
                 "maxfraieho/kindle-butch-gen/master/deploy.sh) -a</code>\n\n"
                 "Скрипт сам перевірить, чи телефон відповідає вимогам "
                 "(діагностика на старті), і розгорне все: пакети, контейнер, "
                 "компіляцію перекладача, модель.\n\n"
                 "📖 Детальна інструкція для Android:\n"
                 "https://vydra.appwrite.network/install.html\n\n"
                 "💻 <b>Інструкція для десктопної версії x86 (WSL2 / CUDA):</b>\n"
                 "/install_x86 або https://vydra.appwrite.network/install_x86.html\n\n"
                 "⚠️ <b>Тримайте екран увімкненим і Termux відкритим</b> до "
                 "напису «Deployment complete» — Android вбиває важкі фонові "
                 "процеси.\n\n"
                 "Після завершення веб-інтерфейс буде на "
                 "<code>http://localhost:5000</code> (пароль скрипт покаже в "
                 "консолі). Питання — пишіть сюди.\n\n"
                 "📜 Правові застереження (авторське право, донати): "
                 "https://vydra.appwrite.network/legal.html")
        return res.json({"ok": True})

    if cmd == "/install_x86":
        _tg_send(token, chat_id,
                 "🚀 <b>Посібник з розгортання Kindle-Butch-Gen (x86 / Windows 11 / WSL2 / CUDA)</b>\n\n"
                 "Розгортання десктопної версії <b>Kindle-Butch-Gen (x86)</b> для Windows 11 "
                 "(через WSL2 та нативну підтримку NVIDIA CUDA) з використанням <b>Appwrite Sites</b>:\n\n"
                 "<b>📋 Крок 1. Передумови та системні вимоги:</b>\n"
                 "• ОС: Windows 11 + активований <b>WSL2</b> (Ubuntu 24.04 LTS).\n"
                 "• GPU: Відеокарта NVIDIA (наприклад, <b>RTX 3050</b> або новіша) з актуальними драйверами CUDA.\n"
                 "• Інструменти: Git та GitHub CLI (gh).\n\n"
                 "<b>🛠️ Крок 2. Підготовка репозиторію:</b>\n"
                 "<code>git clone https://github.com/maxfraieho/kindle-butch-gen-x86.git\n"
                 "cd kindle-butch-gen-x86</code>\n\n"
                 "<b>🌐 Крок 3. Деплой в Appwrite Sites:</b>\n"
                 "1. Увійдіть до Appwrite Console → <b>Sites</b> → <b>Create site</b>.\n"
                 "2. Підключіть GitHub та оберіть репозиторій <code>kindle-butch-gen-x86</code>.\n\n"
                 "<b>⚙️ Крок 4. Змінні середовища:</b>\n"
                 "• Додайте <code>KBG_WEB_PASSWORD</code> (адміністративний пароль).\n\n"
                 "<b>💻 Крок 5. Локальний запуск бекенду та CUDA:</b>\n"
                 "В терміналі WSL2 (Ubuntu 24.04) запустіть:\n"
                 "<code>bash deploy.sh</code>\n"
                 "<i>(Зкомпілює llama.cpp з -DGGML_CUDA=ON та встановить PyTorch з підтримкою CUDA --index-url https://download.pytorch.org/whl/cu121)</i>\n\n"
                 "<b>✅ Крок 6. Перевірка працездатності:</b>\n"
                 "Перевірте статус в Appwrite Sites (ready) та авторизуйтесь у веб-панелі на <code>http://localhost:5000</code>.\n\n"
                 "📖 <b>Повний детальний мануал на сайті:</b>\n"
                 "https://vydra.appwrite.network/install_x86.html")
        return res.json({"ok": True})

    if cmd == "/menu":
        _tg_send(token, chat_id, "🦦 Меню Vydra:", keyboard=_main_keyboard())
        return res.json({"ok": True})

    if cmd == "/donate_dev":
        # STUB: personal donation channel (monobank jar / BMC) not connected
        # yet - integration planned; keep the Track A/B separation wording.
        _tg_send(token, chat_id,
                 "☕ <b>Підтримка розробника</b>\n\n"
                 "Цей канал ще підключається (банка/сервіс оплати буде "
                 "додано незабаром). Це окремий трек — НЕ воєнний збір.\n\n"
                 "🇺🇦 Допомогти захисникам можна вже зараз — офіційний фонд "
                 "з публічною звітністю:\nhttps://savelife.in.ua/donate/")
        return res.json({"ok": True})

    if cmd == "/premium":
        # TASK-53: premium features = donation-gated entitlements
        # (cast_registry - unlocks both Cast Registry and the agent-editor;
        # TASK-56 removed vision_qa as a stale duplicate name for the same
        # thing). Payments service not integrated yet -
        # unlock flow is manual admin /grant after a confirmed donation.
        user = _get_user(db, tg_id)
        ents = (user.get("entitlements") or "") if user else ""
        have = [e for e in ents.split(",") if e]
        status = ("✅ Активні розширені можливості: " + ", ".join(have)) if have \
            else "Розширені можливості поки не активовані."
        _tg_send(token, chat_id,
                 "💳 <b>Розширені можливості Vydra</b>\n\n"
                 f"{status}\n\n"
                 "🧬 <b>Cast Registry</b> — правильний граматичний рід "
                 "персонажів у перекладі (вона зробила, а не він зробив)\n"
                 "👁 <b>Агент-редактор</b> — візуальна перевірка й виправлення "
                 "проблемних сторінок манґи\n"
                 f"📱 <b>Більше {MAX_FREE_DEVICES} пристроїв</b> — безкоштовно "
                 f"можна прив'язати до {MAX_FREE_DEVICES} пристроїв на один акаунт "
                 "(телефон, планшет тощо), кожен наступний потребує розширених "
                 "можливостей\n\n"
                 "Як відкрити: підтримайте проєкт донатом (кнопка "
                 "☕ Підтримати розробника — сервіс оплати вже підключається), "
                 "надішліть сюди підтвердження — і функції буде активовано "
                 "на вашому профілі.\n\n"
                 "⚠️ Технічна примітка: при першому вмиканні Cast Registry "
                 "завантажується додаткова модель (~3–4 GB, разово, на Wi-Fi). "
                 "Генерація книг як була, так і лишається безкоштовною.")
        return res.json({"ok": True})

    if cmd == "/grant":
        # Admin-only: /grant <tg_id|referral_code> <ent1,ent2>
        admin = os.environ.get("ADMIN_TG_ID", "")
        if not admin or str(tg_id) != admin:
            _tg_send(token, chat_id, "Команда доступна лише адміністратору.")
            return res.json({"ok": True})
        parts = arg.split()
        if len(parts) != 2:
            _tg_send(token, chat_id,
                     "Формат: /grant &lt;tg_id або referral_code&gt; "
                     "&lt;cast_registry&gt;")
            return res.json({"ok": True})
        who, ents = parts
        target = None
        for field in ("telegram_id", "referral_code"):
            r2 = db.list_documents(DB_ID, COLL_ID,
                                   queries=[Query.equal(field, who)])
            if r2.get("documents"):
                target = r2["documents"][0]
                break
        if not target:
            _tg_send(token, chat_id, f"Користувача '{who}' не знайдено.")
            return res.json({"ok": True})
        db.update_document(DB_ID, COLL_ID, target["$id"],
                           data={"entitlements": ents})
        _tg_send(token, chat_id,
                 f"✅ Активовано для {target['telegram_id']}: {ents}")
        _tg_send(token, int(target["telegram_id"]),
                 f"🎉 Вам активовано розширені можливості: {ents}. Дякуємо за "
                 "підтримку!\n\nПеремикачі з'являться в налаштуваннях книги. "
                 "⚠️ При першому вмиканні Cast Registry на пристрій "
                 "завантажиться додаткова модель аналізу (~3–4 GB) — "
                 "зробіть це на Wi-Fi.")
        return res.json({"ok": True})

    _tg_send(token, chat_id, "🦦 Меню Vydra:", keyboard=_main_keyboard())
    return res.json({"ok": True})
