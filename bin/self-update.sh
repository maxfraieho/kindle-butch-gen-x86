#!/usr/bin/env bash
# One-button self-update for kindle-butch-gen (TASK-46).
set -uo pipefail

KBG_HOME="$HOME/kindle-butch-gen"
LOG="$HOME/kbg-update.log"
REMOTE="${UPDATE_REMOTE:-server-projects}"

{
    echo ""
    echo "=== self-update started $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
    cd "$KBG_HOME" || { echo "FATAL: $KBG_HOME missing"; exit 1; }

    echo "Attempting pull from remote: $REMOTE"
    if ! git pull --ff-only "$REMOTE" master 2>/dev/null; then
        echo "Pull from $REMOTE failed. Attempting fallback pull from origin..."
        if ! git pull --ff-only origin master 2>/dev/null; then
            echo "FATAL: git pull failed for all remotes - service left untouched."
            exit 1
        fi
    fi
    echo "Now at: $(git log -1 --format='%h %s')"

    sleep 2

    echo "Restarting Flask web server..."
    pkill -f "python3 kbg_web/app.py" || true
    sleep 1

    bash "$KBG_HOME/bin/start-all-services.sh"

    echo "=== self-update finished $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
} >> "$LOG" 2>&1
