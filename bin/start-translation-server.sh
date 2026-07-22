#!/data/data/com.termux/files/usr/bin/bash
# Script to start llama-server for Hy-MT2-7B translation model on port 8081
# Model: Hy-MT2-7B-Q4_K_M (4.4GB) — translation-specific EN/RU → UK
#
# Stopping any previous instance is the caller's responsibility
# (kbg_web/app.py's /api/models/start stops the old process via the PID
# file before invoking this script) — this script only launches and
# records its own PID. It does not pkill anything itself (see TASK-18:
# a duplicate pkill here used to race against the API-layer pkill and
# could leave two llama-server processes running).

termux-wake-lock

export LD_LIBRARY_PATH="$HOME:/system/lib64:/vendor/lib64:$PREFIX/opt/vendor/lib:$HOME/llama.cpp/build/bin"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODEL=$(python3 -c "import json, os; s_path=os.path.join('${REPO_DIR}', 'global_settings.json'); home=os.path.expanduser('~'); default_m=os.path.join(home, 'models/hy-mt2/Hy-MT2-7B-Q4_K_M.gguf'); print(json.load(open(s_path)).get('translation_model', default_m)) if os.path.exists(s_path) else print(default_m)")
PORT=8081
PID_FILE="${1:-$HOME/llama-server-8081.pid}"

echo "$(date): Запуск моделі перекладу Hy-MT2-7B на порту $PORT..."

cd ~/llama.cpp/build/bin
nohup ./llama-server \
  -m "$MODEL" \
  -c 4096 \
  -ngl 99 \
  --parallel 1 \
  -t 4 \
  --no-mmap \
  --host 0.0.0.0 \
  --port "$PORT" \
  > ~/llama-translation-server.log 2>&1 & disown
SERVER_PID=$!
echo "$SERVER_PID" > "$PID_FILE"
echo "$(date): llama-server started (PID: $SERVER_PID) on port $PORT for Hy-MT2-7B" >> ~/llama-boot.log
echo "PID: $SERVER_PID — waiting for server to be ready..."

# Wait for server to be ready
for i in $(seq 1 60); do
  sleep 2
  if LD_LIBRARY_PATH="" curl -s http://127.0.0.1:$PORT/health | grep -q "ok\|healthy"; then
    echo "Server ready after ${i}*2 seconds!"
    break
  fi
  echo -n "."
done
echo ""
echo "Server status: $(curl -s http://localhost:$PORT/health 2>/dev/null || echo 'not ready yet')"
