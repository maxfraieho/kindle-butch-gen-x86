#!/usr/bin/env bash
# Shared service-startup sequence for kindle-butch-gen. Single source of
# truth called from BOTH:
#   1. ~/.bashrc (fires whenever a new Termux shell session starts - e.g.
#      manually reopening the Termux app after it was killed/crashed)
#   2. ~/.termux/boot/start-services.sh (fires on a genuine Android device
#      boot, via the separate Termux:Boot plugin app - see
#      docs/deployment/termux-boot-setup.md for the manual install step
#      this script alone cannot automate)
# Every step is idempotent (checks for an already-running instance before
# starting one) so it's always safe to re-run, from either trigger, any
# number of times.
set -uo pipefail

KBG_HOME="$HOME/kindle-butch-gen"

# 1. Autostart SSH daemon
if ! pgrep -x "sshd" >/dev/null; then
    sshd
fi

# 2. Autostart Llama Translation Server (Hy-MT2-7B on port 8081)
# Defaults to false to prevent overloading the mobile device on boot.
# Can be enabled explicitly by adding "autostart_llama": true to global_settings.json.
AUTOSTART_LLAMA=$(python3 -c "import json; print(str(json.load(open('$KBG_HOME/global_settings.json')).get('autostart_llama', False)).lower())" 2>/dev/null || echo "false")
if [ "$AUTOSTART_LLAMA" = "true" ]; then
    if ! pgrep -f "llama-server.*8081" >/dev/null; then
        echo "Autostart: Starting llama-server on port 8081..."
        nohup bash "$HOME/start-translation-server.sh" > "$HOME/llama-boot.log" 2>&1 &
    fi
else
    echo "Autostart: llama-server autostart is disabled by configuration (autostart_llama=false)."
fi

# 3. Autostart Flask Web Server (on port 5000)
if ! pgrep -f "python3 kbg_web/app.py" >/dev/null; then
    echo "Autostart: Starting Flask web server on port 5000..."
    termux-wake-lock 2>/dev/null || true
    (cd "$KBG_HOME" && nohup python3 kbg_web/app.py --port 5000 > "$HOME/kbg-flask.log" 2>&1 &)
fi

# 4. Auto-resume a conversion that was still running when the environment
# itself went down (not just this one process) - see kbg_web/app.py's
# _write_active_conversion_state / bin/resume_active_conversion.py for the
# full mechanism. No-ops silently if nothing was interrupted. Confirmed
# working live in production: a genuine Termux crash mid-conversion, on
# restart the interrupted book resumed automatically with no manual steps.
# Guarded against an ALREADY-RUNNING conversion (TASK-46 audit finding):
# this script fires on every new Termux shell session, so without the
# pgrep check, opening a shell while a conversion was genuinely active
# would launch a SECOND copy of the same pipeline racing the first over
# the same output files. The state file's presence alone only means
# "Flask never observed completion" - not "nothing is running".
if [ -f "$KBG_HOME/.active_conversion.json" ]; then
    if pgrep -f "translate_manga.py|run_conversion_batches.py|translate_epub.py" >/dev/null; then
        echo "Autostart: conversion state file present but a conversion is already running - not resuming a duplicate."
    else
        echo "Autostart: Detected an interrupted conversion, resuming..."
        # Step 2 backgrounds its ENTIRE script (including that script's own
        # health-check wait loop) with a single outer '&', so this step has
        # no visibility into whether llama-server has actually finished
        # loading the model yet. Observed live: translate_manga.py started
        # hitting the API within ~1s of boot, got 503 "Loading model" on
        # the first couple of pages, and (before translate_manga.py itself
        # gained retry logic) silently kept the original English text for
        # those pages forever. Gate the resume on the same /health probe
        # start-translation-server.sh uses, capped at 2 minutes; if it's
        # still not ready by then, proceed anyway - translate_manga.py's
        # own retry-with-backoff is the second line of defense.
        (
            for i in $(seq 1 60); do
                if LD_LIBRARY_PATH="" curl -s -m 3 http://127.0.0.1:8081/health 2>/dev/null | grep -q "ok\|healthy"; then
                    break
                fi
                sleep 2
            done
            python3 "$KBG_HOME/bin/resume_active_conversion.py"
        ) > "$HOME/kbg-autoresume.log" 2>&1 &
    fi
fi
