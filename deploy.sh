#!/usr/bin/env bash
# kindle-butch-gen (x86_64 / Windows 11 WSL2 / NVIDIA CUDA) Deployment Script
# Single-command installer for x86 Linux / WSL2 environments with NVIDIA GPU acceleration.

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
            echo "  -a, --autostart  Automatically start background services (llama-server & web server) after installation"
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
echo -e "${BLUE}🚀 Kindle-Butch-Gen (x86 / WSL2 / NVIDIA CUDA) Automatic Deployment${NC}"
echo -e "${BLUE}=====================================================================${NC}"

# Ask interactively if not passed via CLI flag
if [ "$AUTOSTART" = "false" ] && [ -t 0 ]; then
    echo -n -e "${BLUE}[DEPL-x86]${NC} Автоматично запустити сервіси (llama-server та Web UI) після завершення? (Y/n): "
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
# STEP 0: Pre-flight diagnostics
# -------------------------------------------------------------
log "Перевірка системних вимог (Pre-flight diagnostics)..."
DIAG_FAILED=0

diag() {
    case "$1" in
        PASS) echo -e "  ${GREEN}[PASS]${NC} $2 — $3" ;;
        WARN) echo -e "  ${YELLOW}[WARN]${NC} $2 — $3" ;;
        FAIL) echo -e "  ${RED}[FAIL]${NC} $2 — $3"; DIAG_FAILED=1 ;;
    esac
}

# Architecture check
ARCH="$(uname -m)"
case "$ARCH" in
    x86_64|amd64) diag PASS "Архітектура" "x86_64" ;;
    *) diag FAIL "Архітектура" "$ARCH — цей скрипт розроблено для x86_64 (Windows 11 WSL2 / Linux x86)" ;;
esac

# OS check
if [ -f /proc/version ] && grep -qi microsoft /proc/version; then
    diag PASS "Операційна система" "WSL2 (Windows Subsystem for Linux)"
else
    diag PASS "Операційна система" "Linux x86_64 ($(uname -s))"
fi

# Memory check
MEM_KB=$(grep MemTotal /proc/meminfo 2>/dev/null | awk '{print $2}' || echo 0)
MEM_GB=$((MEM_KB / 1024 / 1024))
if [ "$MEM_GB" -ge 14 ]; then
    diag PASS "Оперативна пам'ять" "${MEM_GB}GB"
elif [ "$MEM_GB" -ge 8 ]; then
    diag WARN "Оперативна пам'ять" "${MEM_GB}GB — достатньо, але рекомендується 16GB+ для великих моделей"
else
    diag FAIL "Оперативна пам'ять" "${MEM_GB}GB — потрібно щонайменше 8GB"
fi

# Disk space check
FREE_GB=$(df -Pk "$HOME" 2>/dev/null | awk 'NR==2 {print int($4/1024/1024)}' || echo 0)
if [ "$FREE_GB" -ge 25 ]; then
    diag PASS "Вільне місце" "${FREE_GB}GB"
elif [ "$FREE_GB" -ge 15 ]; then
    diag WARN "Вільне місце" "${FREE_GB}GB — достатньо для старту (моделі + проєкт займають ~10GB)"
else
    diag FAIL "Вільне місце" "${FREE_GB}GB — потрібно щонайменше 15GB вільного місця"
fi

# NVIDIA CUDA GPU check
CUDA_SUPPORTED=false
GPU_NAME=""
if command -v nvidia-smi >/dev/null 2>&1; then
    GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -n 1 || echo "")
    CUDA_VER=$(nvidia-smi 2>/dev/null | grep -i "CUDA Version" | awk '{print $NF}' || echo "")
    if [ -n "$GPU_NAME" ]; then
        CUDA_SUPPORTED=true
        diag PASS "NVIDIA GPU" "$GPU_NAME (CUDA Version: ${CUDA_VER:-знайдено})"
    fi
fi

if [ "$CUDA_SUPPORTED" = "false" ]; then
    if [ -e /dev/nvidia0 ] || command -v nvcc >/dev/null 2>&1; then
        CUDA_SUPPORTED=true
        diag PASS "NVIDIA GPU" "Драйвер /dev/nvidia0 або nvcc виявлено"
    else
        diag WARN "NVIDIA GPU" "nvidia-smi не знайдено — обчислення будуть виконуватися на CPU (повільніше)"
    fi
fi

# Network checks
if curl -s -m 8 -o /dev/null "https://github.com"; then
    diag PASS "Мережа" "github.com доступний"
else
    diag FAIL "Мережа" "github.com недоступний — перевірте з'єднання з інтернетом"
fi

if curl -s -m 8 -o /dev/null "https://huggingface.co"; then
    diag PASS "Мережа" "huggingface.co доступний"
else
    diag WARN "Мережа" "huggingface.co недоступний — завантаження моделей може вимагати VPN/проксі"
fi

if [ "$DIAG_FAILED" -ne 0 ]; then
    error "Діагностика виявила невиконані системні вимоги (позначені FAIL вище). Виправте їх і запустіть скрипт знову."
fi
success "Діагностика успішно пройдена."

# -------------------------------------------------------------
# STEP 1: Install System Packages via Sudo / Apt
# -------------------------------------------------------------
log "Встановлення необхідних системних пакетів Linux..."

SUDO_CMD=""
if [ "$(id -u)" -ne 0 ]; then
    if command -v sudo >/dev/null 2>&1; then
        SUDO_CMD="sudo"
    else
        error "Потрібні привілеї root або sudo для встановлення пакетів."
    fi
fi

if command -v apt-get >/dev/null 2>&1; then
    $SUDO_CMD apt-get update -y
    DEBIAN_FRONTEND=noninteractive $SUDO_CMD apt-get install -y --no-install-recommends \
        git curl wget python3 python3-pip python3-venv python3-dev \
        build-essential cmake ninja-build \
        ffmpeg calibre tesseract-ocr tesseract-ocr-ukr \
        libfreetype6-dev libjpeg-dev zlib1g-dev libpng-dev unrar-free p7zip-full
elif command -v dnf >/dev/null 2>&1; then
    $SUDO_CMD dnf install -y git curl wget python3 python3-pip gcc gcc-c++ cmake ninja-build ffmpeg calibre tesseract tesseract-langpack-ukr
fi

success "Системні пакети успішно встановлені."

# -------------------------------------------------------------
# STEP 2: Clone or Setup Repository
# -------------------------------------------------------------
REPO_URL="https://github.com/maxfraieho/kindle-butch-gen-x86.git"
PROJECT_DIR="$HOME/kindle-butch-gen-x86"

log "Підготовка репозиторію проєкту..."
if [ -d "$PROJECT_DIR/.git" ]; then
    log "Репозиторій вже існує у ${PROJECT_DIR}, оновлюємо..."
    git -C "$PROJECT_DIR" pull --ff-only || true
    success "Репозиторій оновлено."
elif [ "$(pwd)" = "$PROJECT_DIR" ] || [ -f "./deploy.sh" ] && [ -d "./kbg_web" ]; then
    PROJECT_DIR="$(pwd)"
    log "Використовуємо поточну директорію: ${PROJECT_DIR}"
else
    log "Клонування ${REPO_URL} у ${PROJECT_DIR}..."
    git clone "$REPO_URL" "$PROJECT_DIR"
    success "Репозиторій клоновано."
fi

cd "$PROJECT_DIR"
chmod +x kbg.sh || true

# -------------------------------------------------------------
# STEP 3: Compile llama.cpp with CUDA Support
# -------------------------------------------------------------
log "Збіркаllama.cpp з підтримкою NVIDIA CUDA..."

if [ -x "$HOME/llama.cpp/build/bin/llama-server" ]; then
    success "Зкомпільований llama.cpp вже присутній (~/llama.cpp/build/bin/llama-server) — пропуск збірки."
else
    log "Клонування та збірка llama.cpp (з прапорцем -DGGML_CUDA=ON)..."
    if [ ! -d "$HOME/llama.cpp/.git" ]; then
        git clone --depth 1 https://github.com/ggerganov/llama.cpp.git "$HOME/llama.cpp"
    fi
    cd "$HOME/llama.cpp"
    rm -rf build
    mkdir -p build && cd build

    CMAKE_FLAGS="-DLLAMA_CURL=OFF"
    if [ "$CUDA_SUPPORTED" = "true" ]; then
        CMAKE_FLAGS="$CMAKE_FLAGS -DGGML_CUDA=ON"
        log "Увімкнено прискорення NVIDIA CUDA (-DGGML_CUDA=ON)"
    else
        log "CUDA не виявлено — збірка в режимі CPU"
    fi

    cmake .. $CMAKE_FLAGS
    make -j"$(nproc)" llama-server llama-cli

    cd "$HOME"
    [ -x "$HOME/llama.cpp/build/bin/llama-server" ] || error "Помилка: бінарний файл llama-server відсутній після збірки!"
    success "llama.cpp успішно зкомпільовано."
fi

# Install start script helper to $HOME
if [ ! -f "$HOME/start-translation-server.sh" ]; then
    cp "$PROJECT_DIR/bin/start-translation-server.sh" "$HOME/start-translation-server.sh"
    chmod +x "$HOME/start-translation-server.sh"
    success "start-translation-server.sh скопійовано у $HOME."
fi

# -------------------------------------------------------------
# STEP 4: Setup Python Environment & Dependencies
# -------------------------------------------------------------
log "Налаштування Python залежностей (PyTorch CUDA, Transformers, Marker, Manga-OCR)..."

cd "$PROJECT_DIR"
pip install --upgrade pip || true

if [ "$CUDA_SUPPORTED" = "true" ]; then
    log "Встановлення PyTorch з підтримкою CUDA 12.1..."
    pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121 --ignore-installed
else
    log "Встановлення PyTorch (CPU версія)..."
    pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cpu --ignore-installed
fi

log "Встановлення інших необхідних пакетів Python..."
pip install Flask flask-httpauth requests tqdm marisa-trie blinker Pillow pytesseract num2words opencv-python-headless pyyaml || true

if [ -f "requirements.txt" ]; then
    pip install -r requirements.txt || true
fi

success "Python залежності успішно встановлені."

# -------------------------------------------------------------
# STEP 5: Download Premium Models
# -------------------------------------------------------------
log "Перевірка моделей ШІ..."
mkdir -p "$HOME/models/hy-mt2" "$HOME/models/sherpa-onnx-whisper-small-int8" "$HOME/models/gemma3-4b"

MODEL_HY="$HOME/models/hy-mt2/Hy-MT2-7B-Q4_K_M.gguf"
if [ ! -f "$MODEL_HY" ] || [ $(wc -c < "$MODEL_HY" 2>/dev/null || echo 0) -lt 1000000000 ]; then
    log "Завантаження моделі перекладу Hy-MT2-7B-Q4_K_M (~4.4 GB)..."
    curl -L -C - -o "$MODEL_HY" "https://huggingface.co/TencentARC/Hy-MT2-7B-GGUF/resolve/main/Hy-MT2-7B-Q4_K_M.gguf" || warn "Завантаження моделі Hy-MT2-7B можна завершити пізніше через bin/download_premium_models.sh"
fi

if [ -f "$PROJECT_DIR/bin/download_premium_models.sh" ]; then
    log "Запуск скрипту завантаження додаткових преміум-моделей (ASR Whisper, Gemma)..."
    bash "$PROJECT_DIR/bin/download_premium_models.sh" --all || true
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
# STEP 6: Launch Services if Autostart Enabled
# -------------------------------------------------------------
if [ "$AUTOSTART" = "true" ]; then
    log "Запуск фонових сервісів (llama-server та Flask Web UI)..."
    
    # 1. Start llama-server on port 8081 if not running
    if ! pgrep -f "llama-server.*8081" >/dev/null; then
        log "Запуск llama-server на порту 8081..."
        nohup bash "$HOME/start-translation-server.sh" > "$HOME/llama-boot.log" 2>&1 &
    fi

    # 2. Start Flask Web UI on port 5000 if not running
    if ! pgrep -f "python3 kbg_web/app.py" >/dev/null; then
        log "Запуск веб-панелі Flask на порту 5000..."
        (cd "$PROJECT_DIR" && nohup python3 kbg_web/app.py --port 5000 > "$HOME/kbg-flask.log" 2>&1 &)
    fi
fi

# -------------------------------------------------------------
# SUCCESS BANNER
# -------------------------------------------------------------
echo -e "${GREEN}=====================================================================${NC}"
echo -e "${GREEN}🎉 ВСТАНОВЛЕННЯ KINDLE-BUTCH-GEN (x86 / CUDA) УСПІШНО ЗАВЕРШЕНО!${NC}"
echo -e "${GREEN}=====================================================================${NC}"
echo -e " 🌐 Веб-панель керування:  ${BLUE}http://localhost:5000${NC}"
echo -e " 🤖 Модельний сервер LLM:   ${BLUE}http://localhost:8081${NC}"
echo -e " 📂 Каталог проєкту:         ${BLUE}${PROJECT_DIR}${NC}"
echo -e " 📖 Керування через CLI:     ${BLUE}cd ${PROJECT_DIR} && ./kbg.sh status <slug>${NC}"
echo -e "${GREEN}=====================================================================${NC}"

