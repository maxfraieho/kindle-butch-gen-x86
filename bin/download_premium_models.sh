#!/data/data/com.termux/files/usr/bin/bash
# Premium AI-model downloader for kindle-butch-gen:
# - Gemma 3 4B (~2.5GB) + mmproj (~850MB) for Agent-Editor / Cast Registry
# - Whisper Small INT8 (~245MB) for ASR accent verification loop
#
# Supports flags:
#   --all         Download all models (default if no flags given)
#   --gemma       Download Gemma 3 4B vision models only
#   --asr         Download Whisper Small INT8 ASR models only
#   --whisper     Alias for --asr
set -uo pipefail

TARGET="all"
while [ $# -gt 0 ]; do
    case "$1" in
        --gemma) TARGET="gemma"; shift ;;
        --asr|--whisper) TARGET="asr"; shift ;;
        --all) TARGET="all"; shift ;;
        *) TARGET="$1"; shift ;;
    esac
done
if [ "$TARGET" = "whisper" ]; then TARGET="asr"; fi

CONSENT_GIVE="${CONSENT_ACCEPTED:-${GEMMA_TERMS_ACCEPTED:-0}}"
if [ "$CONSENT_GIVE" != "1" ]; then
    echo ""
    echo "Ці розширені функції використовують додаткові нейромережеві моделі."
    echo "Завантажуючи ваги, ви приймаєте умови використання моделей:"
    echo "  - Google Gemma Terms of Use & Prohibited Use Policy (https://ai.google.dev/gemma/terms)"
    echo "  - OpenAI Whisper / sherpa-onnx License (MIT/Apache 2.0)"
    if [ -t 0 ]; then
        printf "Приймаєте умови та дозвіл на завантаження моделей? [y/N]: "
        read -r ans
        case "$ans" in [Yy]*) : ;; *) echo "Скасовано."; exit 1 ;; esac
    else
        echo "ВІДМОВА: згода не надана (CONSENT_ACCEPTED!=1). Запустіть через UI з підтвердженням."
        exit 1
    fi
fi

fetch_and_verify() {
    local target_dir="$1"
    local filename="$2"
    local url="$3"
    local min_bytes="$4"

    mkdir -p "$target_dir"
    local part_file="$target_dir/$filename.part"
    local final_file="$target_dir/$filename"

    if [ -f "$final_file" ]; then
        local sz
        sz=$(wc -c < "$final_file" 2>/dev/null || echo 0)
        if [ "$sz" -ge "$min_bytes" ]; then
            echo "[premium-models] $filename вже завантажено ($sz байт) — пропуск."
            return 0
        fi
        echo "[premium-models] $filename розмір ($sz) менший за мінімальний ($min_bytes) — перезавантаження."
    fi

    echo "[premium-models] Завантаження $filename з $url..."
    curl -L -C - --fail --retry 5 --retry-delay 2 -o "$part_file" "$url"
    local downloaded_sz
    downloaded_sz=$(wc -c < "$part_file" 2>/dev/null || echo 0)
    if [ "$downloaded_sz" -lt "$min_bytes" ]; then
        echo "[premium-models] ПОМИЛКА: Розмір $filename ($downloaded_sz байт) менший за потрібний ($min_bytes байт)." >&2
        rm -f "$part_file"
        return 1
    fi
    mv "$part_file" "$final_file"
    echo "[premium-models] $filename ГОТОВО ($downloaded_sz байт підтверджено)."
}

# 1. Gemma Models
if [ "$TARGET" = "all" ] || [ "$TARGET" = "gemma" ]; then
    GEMMA_DIR="$HOME/models/gemma3-4b"
    GEMMA_BASE="https://huggingface.co/ggml-org/gemma-3-4b-it-GGUF/resolve/main"
    echo "[premium-models] Завантаження моделей Gemma 3 4B у $GEMMA_DIR"
    fetch_and_verify "$GEMMA_DIR" "gemma-3-4b-it-Q4_K_M.gguf" "$GEMMA_BASE/gemma-3-4b-it-Q4_K_M.gguf" 2000000000
    fetch_and_verify "$GEMMA_DIR" "mmproj-model-f16.gguf" "$GEMMA_BASE/mmproj-model-f16.gguf" 700000000
fi

# 2. Whisper ASR Models
if [ "$TARGET" = "all" ] || [ "$TARGET" = "asr" ]; then
    WHISPER_DIR="$HOME/models/sherpa-onnx-whisper-small-int8"
    WHISPER_BASE="https://huggingface.co/csukuangfj/sherpa-onnx-whisper-small/resolve/main"
    echo "[premium-models] Завантаження моделей Whisper Small INT8 у $WHISPER_DIR"
    fetch_and_verify "$WHISPER_DIR" "small-encoder.int8.onnx" "$WHISPER_BASE/small-encoder.int8.onnx" 100000000
    fetch_and_verify "$WHISPER_DIR" "small-decoder.int8.onnx" "$WHISPER_BASE/small-decoder.int8.onnx" 40000000
    fetch_and_verify "$WHISPER_DIR" "small-tokens.txt" "$WHISPER_BASE/small-tokens.txt" 100000
fi

echo "[premium-models] Усі запитані моделі готові до використання."
