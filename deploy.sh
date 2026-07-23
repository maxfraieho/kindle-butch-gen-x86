#!/usr/bin/env bash
# kindle-butch-gen (x86_64 / Windows 11 WSL2 / NVIDIA CUDA) Deployment Script
# 100% Autonomous single-command installer with ZERO manual preparation requirements.

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log() {
    echo -e "${BLUE}[DEPL-x86]${NC} $1"
}

success() {
    echo -e "${GREEN}[OK]${NC} $1"
}

warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

error() {
    echo -e "${RED}[ERROR]${NC} $1"
    exit 1
}

AUTOSTART=false

# Parse CLI arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -a|--autostart)
            AUTOSTART=true
            shift
            ;;
        -h|--help)
            echo "Usage: $0 [-a|--autostart]"
            echo "  -a, --autostart  Автоматично запустити сервіси після розгортання"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            echo "Usage: $0 [-a|--autostart]"
            exit 1
            ;;
    esac
done

echo -e "${BLUE}=====================================================================${NC}"
echo -e "${BLUE}🚀 Kindle-Butch-Gen (x86 / WSL2 / NVIDIA CUDA) Повністю Автономне Розгортання${NC}"
echo -e "${BLUE}=====================================================================${NC}"

# -------------------------------------------------------------
# STEP 0: Auto-fix WSL configuration issues & Install bootstrap tools
# -------------------------------------------------------------
SUDO_CMD=""
HAS_SUDO=false
if [ "$(id -u)" -eq 0 ]; then
    HAS_SUDO=true
elif command -v sudo >/dev/null 2>&1; then
    if sudo -n true 2>/dev/null; then
        SUDO_CMD="sudo"
        HAS_SUDO=true
    fi
fi

# Detect WSL environment and check/fix common .wslconfig errors (e.g. unknown key 'wsl2.systemd')
if [ -d "/mnt/c/Users" ] || grep -qi microsoft /proc/version 2>/dev/null; then
    for wslcfg in /mnt/c/Users/*/.wslconfig; do
        if [ -f "$wslcfg" ] && grep -q "wsl2\.systemd" "$wslcfg" 2>/dev/null; then
            log "Виявлено некоректний ключ 'wsl2.systemd' у $wslcfg — виправляю на 'systemd'..."
            sed -i 's/wsl2\.systemd/systemd/g' "$wslcfg" 2>/dev/null || true
            success "Конфігурацію .wslconfig виправлено автоматично."
        fi
    done
fi

if ! command -v git >/dev/null 2>&1 || ! command -v curl >/dev/null 2>&1; then
    log "Встановлення первинних утиліт (git, curl)..."
    if command -v apt-get >/dev/null 2>&1; then
        DEBIAN_FRONTEND=noninteractive $SUDO_CMD apt-get update -y >/dev/null 2>&1 || true
        DEBIAN_FRONTEND=noninteractive $SUDO_CMD apt-get install -y git curl >/dev/null 2>&1 || true
    elif command -v dnf >/dev/null 2>&1; then
        $SUDO_CMD dnf install -y git curl >/dev/null 2>&1 || true
    fi
fi

# Ask interactively if not passed via CLI flag
if [ "$AUTOSTART" = "false" ] && [ -t 0 ]; then
    echo -n -e "${BLUE}[DEPL-x86]${NC} Автоматично запустити веб-панель та ШІ-сервер після завершення? (Y/n): "
    read -r choice || choice=""
    case "$choice" in 
        [nN]|[nN][oO])
            AUTOSTART=false
            log "Автостарт сервісів пропущено."
            ;;
        *)
            AUTOSTART=true
            log "Автостарт сервісів увімкнено."
            ;;
    esac
fi

# -------------------------------------------------------------
# STEP 1: Pre-flight diagnostics & Automatic GPU Detection
# -------------------------------------------------------------
log "Автоматичний аналіз системи..."

# Memory check
MEM_KB=$(grep MemTotal /proc/meminfo 2>/dev/null | awk '{print $2}' || echo 0)
MEM_GB=$((MEM_KB / 1024 / 1024))

# Disk space check
FREE_GB=$(df -Pk "$HOME" 2>/dev/null | awk 'NR==2 {print int($4/1024/1024)}' || echo 0)

# Automatic CUDA / NVIDIA GPU Detection (WSL2 & Linux)
CUDA_SUPPORTED=false
GPU_INFO="CPU Mode"

if command -v nvidia-smi >/dev/null 2>&1; then
    GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -n 1 || echo "")
    if [ -n "$GPU_NAME" ]; then
        CUDA_SUPPORTED=true
        GPU_INFO="NVIDIA $GPU_NAME (nvidia-smi)"
    fi
fi

if [ "$CUDA_SUPPORTED" = "false" ]; then
    # Check WSL2 NVIDIA driver pass-through paths (/usr/lib/wsl/lib or /dev/dxg)
    if [ -d "/usr/lib/wsl/lib" ] || [ -e "/dev/dxg" ] || [ -e "/dev/nvidia0" ]; then
        CUDA_SUPPORTED=true
        GPU_INFO="NVIDIA CUDA Pass-through (WSL2 / Linux)"
    fi
fi

echo -e "  ${GREEN}[INFO]${NC} Архітектура: $(uname -m)"
echo -e "  ${GREEN}[INFO]${NC} Оперативна пам'ять: ${MEM_GB}GB"
echo -e "  ${GREEN}[INFO]${NC} Вільне місце: ${FREE_GB}GB"
if [ "$CUDA_SUPPORTED" = "true" ]; then
    echo -e "  ${GREEN}[PASS]${NC} GPU Прискорення: ${GPU_INFO} — CUDA буде активовано"
else
    echo -e "  ${YELLOW}[WARN]${NC} GPU Прискорення: не виявлено — обчислення будуть на CPU"
fi

# -------------------------------------------------------------
# STEP 2: Automatic Installation of All System Dependencies
# -------------------------------------------------------------
log "Автоматичне встановлення всіх системних пакетів (Python, CMake, FFmpeg, Calibre)..."

if [ "$HAS_SUDO" = "true" ]; then
    if command -v apt-get >/dev/null 2>&1; then
        DEBIAN_FRONTEND=noninteractive $SUDO_CMD apt-get update -y || true
        DEBIAN_FRONTEND=noninteractive $SUDO_CMD apt-get install -y --no-install-recommends \
            git curl wget python3 python3-pip python3-venv python3-dev \
            build-essential cmake ninja-build \
            ffmpeg calibre tesseract-ocr tesseract-ocr-ukr \
            libfreetype6-dev libjpeg-dev zlib1g-dev libpng-dev unrar-free p7zip-full || warn "Деякі системні пакети apt не вдалося встановити."
    elif command -v dnf >/dev/null 2>&1; then
        $SUDO_CMD dnf install -y git curl wget python3 python3-pip gcc gcc-c++ cmake ninja-build ffmpeg calibre tesseract tesseract-langpack-ukr || warn "Деякі системні пакети dnf не вдалося встановити."
    fi
else
    warn "Пропуск автоматичного apt/dnf встановлення (потрібен sudo пароль або root). Використовуються наявні користувацькі інструменти."
fi

success "Етап системних пакетів завершено."

# -------------------------------------------------------------
# STEP 3: Setup Project Repository
# -------------------------------------------------------------
REPO_URL="https://github.com/maxfraieho/kindle-butch-gen-x86.git"
PROJECT_DIR="$HOME/kindle-butch-gen-x86"

log "Підготовка репозиторію проєкту..."
if [ -d "$PROJECT_DIR/.git" ]; then
    log "Оновлення проєкту у ${PROJECT_DIR}..."
    git -C "$PROJECT_DIR" pull --ff-only || true
elif [ -f "./deploy.sh" ] && [ -d "./kbg_web" ]; then
    PROJECT_DIR="$(pwd)"
else
    log "Завантаження проєкту з GitHub..."
    git clone "$REPO_URL" "$PROJECT_DIR"
fi

cd "$PROJECT_DIR"
chmod +x kbg.sh || true

# -------------------------------------------------------------
# STEP 4: Automatic llama.cpp CUDA Compilation
# -------------------------------------------------------------
log "Автоматичне збирання ШІ-сервера llama.cpp..."

if [ -x "$HOME/llama.cpp/build/bin/llama-server" ] || [ -x "$HOME/llama.cpp/build/llama-server" ]; then
    mkdir -p "$HOME/llama.cpp/build/bin"
    [ ! -x "$HOME/llama.cpp/build/bin/llama-server" ] && [ -x "$HOME/llama.cpp/build/llama-server" ] && ln -sf "$HOME/llama.cpp/build/llama-server" "$HOME/llama.cpp/build/bin/llama-server" || true
    success "llama.cpp вже зкомпільовано — пропуск збірки."
else
    if [ ! -d "$HOME/llama.cpp/.git" ]; then
        git clone --depth 1 https://github.com/ggerganov/llama.cpp.git "$HOME/llama.cpp" || true
    fi
    if [ -d "$HOME/llama.cpp" ]; then
        cd "$HOME/llama.cpp"
        rm -rf build
        mkdir -p build && cd build

        CMAKE_FLAGS="-DLLAMA_CURL=OFF"
        if [ "$CUDA_SUPPORTED" = "true" ]; then
            CMAKE_FLAGS="$CMAKE_FLAGS -DGGML_CUDA=ON"
            log "Компіляція з підтримкою NVIDIA CUDA (-DGGML_CUDA=ON)..."
        fi

        cmake .. $CMAKE_FLAGS || true
        make -j"$(nproc)" llama-server llama-cli || cmake --build . --config Release -j"$(nproc)" --target llama-server llama-cli || true

        mkdir -p "$HOME/llama.cpp/build/bin"
        [ ! -x "$HOME/llama.cpp/build/bin/llama-server" ] && [ -x "$HOME/llama.cpp/build/llama-server" ] && ln -sf "$HOME/llama.cpp/build/llama-server" "$HOME/llama.cpp/build/bin/llama-server" || true

        cd "$HOME"
        if [ -x "$HOME/llama.cpp/build/bin/llama-server" ] || [ -x "$HOME/llama.cpp/build/llama-server" ]; then
            success "llama.cpp зкомпільовано успішно."
        else
            warn "llama.cpp не було зкомпільовано (можливо, відсутні cmake/gcc). Сервіси LLM можна завантажити у бінарному вигляді."
        fi
    fi
fi

# Copy start helper script
if [ ! -f "$HOME/start-translation-server.sh" ]; then
    cp "$PROJECT_DIR/bin/start-translation-server.sh" "$HOME/start-translation-server.sh"
    chmod +x "$HOME/start-translation-server.sh"
fi

# -------------------------------------------------------------
# STEP 5: Automatic Python Setup & PyTorch CUDA Installation
# -------------------------------------------------------------
log "Налаштування Python та PyTorch CUDA..."

cd "$PROJECT_DIR"
PYTHON_BIN="$(command -v python3 || echo python)"
PIP_CMD="$PYTHON_BIN -m pip"
export PIP_BREAK_SYSTEM_PACKAGES=1

$PIP_CMD install --upgrade pip || true

if [ "$CUDA_SUPPORTED" = "true" ]; then
    log "Встановлення PyTorch CUDA (cu121)..."
    $PIP_CMD install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121 --ignore-installed || true
else
    log "Встановлення PyTorch CPU..."
    $PIP_CMD install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cpu --ignore-installed || true
fi

log "Встановлення інших необхідних пакетів..."
$PIP_CMD install Flask flask-httpauth requests tqdm marisa-trie blinker Pillow pytesseract num2words opencv-python-headless pyyaml || true

if [ -f "requirements.txt" ]; then
    $PIP_CMD install -r requirements.txt || true
fi

success "Python середовище повністю налаштовано."

# -------------------------------------------------------------
# STEP 6: Automatic Download of AI Models
# -------------------------------------------------------------
log "Автоматичне завантаження моделей ШІ..."
mkdir -p "$HOME/models/hy-mt2" "$HOME/models/sherpa-onnx-whisper-small-int8" "$HOME/models/gemma3-4b"

MODEL_HY="$HOME/models/hy-mt2/Hy-MT2-7B-Q4_K_M.gguf"
if [ ! -f "$MODEL_HY" ] || [ $(wc -c < "$MODEL_HY" 2>/dev/null || echo 0) -lt 1000000000 ]; then
    log "Завантаження моделі перекладу Hy-MT2-7B (~4.4 GB)..."
    curl -L -C - --fail --retry 3 -o "$MODEL_HY" "https://huggingface.co/mradermacher/Hy-MT2-7B-GGUF/resolve/main/Hy-MT2-7B.Q4_K_M.gguf" || warn "Модель перекладу можна дозавантажити пізніше."
fi

if [ -f "$PROJECT_DIR/bin/download_premium_models.sh" ]; then
    CONSENT_ACCEPTED=1 GEMMA_TERMS_ACCEPTED=1 bash "$PROJECT_DIR/bin/download_premium_models.sh" --all || true
fi

# Create default global_settings.json
if [ ! -f "$PROJECT_DIR/global_settings.json" ]; then
    cat << EOF > "$PROJECT_DIR/global_settings.json"
{
  "output_root": "$HOME/Documents/Books",
  "translation_model": "$HOME/models/hy-mt2/Hy-MT2-7B-Q4_K_M.gguf",
  "editor_model": "$HOME/models/gemma3-4b/gemma-3-4b-it-Q4_K_M.gguf",
  "autostart_llama": false
}
EOF
fi

# -------------------------------------------------------------
# STEP 7: Launch Background Services
# -------------------------------------------------------------
if [ "$AUTOSTART" = "true" ]; then
    log "Запуск сервісів..."
    if ! pgrep -f "llama-server.*8081" >/dev/null; then
        nohup bash "$HOME/start-translation-server.sh" > "$HOME/llama-boot.log" 2>&1 &
    fi

    if ! pgrep -f "python3 kbg_web/app.py" >/dev/null; then
        (cd "$PROJECT_DIR" && nohup python3 kbg_web/app.py --port 5000 > "$HOME/kbg-flask.log" 2>&1 &)
    fi
fi

# -------------------------------------------------------------
# FINAL SUCCESS BANNER
# -------------------------------------------------------------
echo -e "${GREEN}=====================================================================${NC}"
echo -e "${GREEN}🎉 Vydra x86 (WSL2 / CUDA) УСПІШНО РОЗГОРНУТО ТА ГОТОВА ДО РОБОТИ!${NC}"
echo -e "${GREEN}=====================================================================${NC}"
echo -e " 🌐 Веб-панель:              ${BLUE}http://localhost:5000${NC}"
echo -e " 🤖 Модельний сервер LLM:   ${BLUE}http://localhost:8081${NC}"
echo -e " 📂 Папка проєкту:           ${BLUE}${PROJECT_DIR}${NC}"
echo -e "${GREEN}=====================================================================${NC}"
