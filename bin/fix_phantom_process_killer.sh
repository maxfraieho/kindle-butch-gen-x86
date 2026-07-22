#!/data/data/com.termux/files/usr/bin/bash
# Raises Android's "phantom process" limit so it stops silently killing
# Vydra's translation/audio/agent processes in the background - a
# SEPARATE mechanism from Battery Optimization (see docs/uk/install.md
# Крок 2), introduced in Android 12+, that can kill a background app's
# process tree even when that app is explicitly exempted from battery
# optimization ("Без обмежень"). Observed live 2026-07-19: a phone with
# battery optimization already disabled for Termux still had its
# translation killed roughly every ~30 minutes, consistently - the
# signature of this specific limiter, not Doze/background-execution-limits.
#
# This requires ADB shell access ONCE, which requires enabling Android's
# Wireless Debugging (Developer Options) and pairing - this script talks
# you through both steps, no PC or second device needed (Android 11+
# lets a device debug itself over its own Wi-Fi loopback).
set -uo pipefail

log() { echo "$1"; }

log "=== Vydra: фікс 'вбивці фонових процесів' Android ==="
log ""
log "Це одноразове налаштування. Без нього Android (Android 12+, деякі"
log "виробники — навіть на старіших версіях) може вбивати переклад/"
log "озвучення/агента приблизно кожні ~30 хвилин, НАВІТЬ якщо ви вже"
log "вимкнули оптимізацію батареї для Termux (це окремий механізм)."
log ""

if ! command -v adb >/dev/null 2>&1; then
    log "Встановлюю android-tools (adb)..."
    pkg install -y android-tools || { log "Помилка встановлення android-tools."; exit 1; }
fi

log ""
log "КРОК 1. Увімкніть Wireless debugging:"
log "  Налаштування → Про телефон → натисніть 'Номер збірки' 7 разів"
log "  (якщо розробницькі опції ще не відкриті)"
log "  Налаштування → Система → Для розробників → Бездротове налагодження → УВІМКНУТИ"
log ""
log "КРОК 2. На тому ж екрані тапніть на напис 'Бездротове налагодження'"
log "  (не на перемикач) → 'Пара пристрою за кодом' (Pair device with pairing code)."
log "  З'явиться щось на кшталт: IP-адреса та порт: 192.168.X.X:XXXXX"
log "  Код пари: XXXXXX"
log ""
read -rp "Введіть IP:порт для СПАРУВАННЯ (з екрана 'Пара пристрою за кодом'): " PAIR_ADDR
read -rp "Введіть 6-значний код пари: " PAIR_CODE

log ""
log "Спарювання..."
if ! adb pair "$PAIR_ADDR" "$PAIR_CODE"; then
    log "Спарювання не вдалося. Перевірте, що код і адреса ще дійсні (вони"
    log "оновлюються щоразу, як відкриваєте цей екран) і спробуйте знову."
    exit 1
fi

log ""
log "КРОК 3. Поверніться на головний екран 'Бездротове налагодження'"
log "  (закрийте діалог спарювання). Там побачите ІНШУ IP-адресу:порт"
log "  (не ту, що для спарювання) — це адреса для підключення."
read -rp "Введіть IP:порт для ПІДКЛЮЧЕННЯ: " CONNECT_ADDR

log ""
log "Підключення..."
if ! adb connect "$CONNECT_ADDR"; then
    log "Підключення не вдалося."
    exit 1
fi

log ""
log "Застосовую фікс..."
adb -s "$CONNECT_ADDR" shell device_config put activity_manager max_phantom_processes 2147483647

RESULT=$(adb -s "$CONNECT_ADDR" shell device_config get activity_manager max_phantom_processes 2>&1)
log ""
if echo "$RESULT" | grep -q "2147483647"; then
    log "✅ Готово! max_phantom_processes = 2147483647 (без обмеження)."
    log "Переклад/озвучення/агент більше не мають зникати через цей механізм."
else
    log "⚠️ Не вдалося підтвердити зміну (отримано: $RESULT)."
    log "Спробуйте команду вручну: adb shell device_config put activity_manager max_phantom_processes 2147483647"
fi

log ""
log "Можна вимкнути 'Бездротове налагодження' назад у налаштуваннях —"
log "зміна залишається чинною і без нього, аж до заводського скидання."
