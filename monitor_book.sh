#!/bin/bash
# monitor_book.sh - Monitors book conversion progress and restarts if stuck
# Usage: bash monitor_book.sh three-days-of-happiness
SLUG="${1:-three-days-of-happiness}"
# Credentials must never be hardcoded here (this script is public git
# history) - read the same env vars app.py itself uses, matching
# whatever the current live web login actually is instead of a stale
# baked-in value that silently stops working (or leaks an old secret)
# the moment the password is rotated.
TERMUX_HOME="${TERMUX_HOME:-$HOME}"
WEB_USER="${KBG_WEB_USER:-admin}"
WEB_PASSWORD="${KBG_WEB_PASSWORD:?Вкажіть KBG_WEB_PASSWORD перед запуском monitor_book.sh}"
AUTH="${WEB_USER}:${WEB_PASSWORD}"
BASE_URL="http://localhost:5000"
LLAMA_URL="http://localhost:8081"
LLAMA_MODEL="${LLAMA_MODEL:-$TERMUX_HOME/models/hy-mt2/Hy-MT2-7B-Q4_K_M.gguf}"
LLAMA_SERVER="${LLAMA_SERVER:-$TERMUX_HOME/llama.cpp/build/bin/llama-server}"
CHECK_INTERVAL=60   # seconds between checks
STALL_LIMIT=6       # checks with no progress = 6min stall → restart

prev_pct="-1"
stall_count=0

log() { echo "[$(date '+%H:%M:%S')] $*"; }

check_llama() {
    python3 -c "
import requests
try:
    r = requests.get('${LLAMA_URL}/health', timeout=5)
    print('ok' if r.status_code == 200 else 'fail')
except: print('fail')
" 2>/dev/null
}

restart_llama() {
    log "Перезапуск llama-server..."
    pkill -f "llama-serve[r]" 2>/dev/null
    sleep 3
    nohup "$LLAMA_SERVER" \
        -m "$LLAMA_MODEL" -c 4096 -ngl 99 -t 8 \
        --host 0.0.0.0 --port 8081 \
        > /tmp/llama-server.log 2>&1 &
    log "PID сервера llama-server: $!"
    sleep 10
}

restart_conversion() {
    log "Перезапуск конвертації для $SLUG..."
    python3 -c "
import requests
from requests.auth import HTTPBasicAuth
auth = HTTPBasicAuth('${WEB_USER}', '${WEB_PASSWORD}')
r = requests.post('${BASE_URL}/api/run/${SLUG}',
    auth=auth, headers={'Content-Type': 'application/json'}, json={}, timeout=15)
print(r.status_code, r.text[:100])
"
}

log "=== Запуск моніторингу для книги: $SLUG ==="

while true; do
    # Get status
    STATUS=$(python3 -c "
import requests, json
from requests.auth import HTTPBasicAuth
try:
    auth = HTTPBasicAuth('${WEB_USER}', '${WEB_PASSWORD}')
    r = requests.get('${BASE_URL}/api/status/${SLUG}', auth=auth, timeout=10)
    d = r.json()
    p = d.get('progress', {})
    running = d.get('is_running', False)
    t = p.get('translation_percent', 0)
    s = p.get('stress_percent', 0)
    tts = p.get('tts_percent', 0)
    e = p.get('edit_percent', 0)
    print(f'{int(running)}|{t:.1f}|{s:.1f}|{tts:.1f}|{e:.1f}')
except Exception as ex:
    print(f'error:{ex}')
" 2>/dev/null)

    if [[ "$STATUS" == error:* ]]; then
        log "Помилка API: $STATUS"
        sleep $CHECK_INTERVAL
        continue
    fi

    IFS='|' read -r running trans stress tts edit <<< "$STATUS"
    log "Запущено=$running | Переклад=${trans}% | Наголоси=${stress}% | TTS=${tts}% | Редагування=${edit}%"

    # Check if fully done
    if (( $(echo "$tts >= 99.9" | python3 -c "import sys; print(int(eval(sys.stdin.read())))") )); then
        log "=== УСІ ЕТАПИ ЗАВЕРШЕНО! Озвучка TTS на ${tts}% ==="
        log "Завершення робочих процесів..."
        pkill -f "llama-serve[r]"
        pkill -f "kbg_web/app[.]py"
        pkill -f "translate_epub[.]py"
        pkill -f "run_conversio[n]"
        log "Готово. Усі сервіси зупинено."
        exit 0
    fi

    # Check llama-server health
    llama_health=$(check_llama)
    if [[ "$llama_health" != "ok" ]]; then
        log "llama-server СТОЇТЬ! Перезапуск..."
        restart_llama
    fi

    # Check for stall
    if [[ "$running" == "0" ]]; then
        stall_count=$((stall_count + 1))
        log "Процес не запущено. Кількість затримок: $stall_count/$STALL_LIMIT"
        if [[ $stall_count -ge $STALL_LIMIT ]]; then
            log "Затримка виконання! Перезапуск конвертації..."
            restart_conversion
            stall_count=0
            sleep 5
        fi
    else
        # Check progress stall
        if [[ "$trans" == "$prev_pct" ]] && (( $(echo "$trans < 99.9" | python3 -c "import sys; print(int(eval(sys.stdin.read())))") )); then
            stall_count=$((stall_count + 1))
            log "Прогрес відсутній. Кількість затримок: $stall_count/$STALL_LIMIT"
            if [[ $stall_count -ge $STALL_LIMIT ]]; then
                log "Прогрес зупинився! Перезапуск..."
                pkill -f "translate_epub[.]py" 2>/dev/null
                sleep 2
                restart_conversion
                stall_count=0
            fi
        else
            stall_count=0
            prev_pct="$trans"
        fi
    fi

    sleep $CHECK_INTERVAL
done
