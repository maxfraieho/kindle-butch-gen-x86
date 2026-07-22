#!/data/data/com.termux/files/usr/bin/bash
# KBG CLI Wrapper

set -e

# Resolve repo root directory
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

validate_slug() {
  local slug="$1"
  if [[ ! "$slug" =~ ^[a-z0-9_-]+$ ]]; then
    echo "Помилка: Недійсний ідентифікатор '$slug'. Повинен відповідати шаблону ^[a-z0-9_-]+$" >&2
    exit 1
  fi
}

show_usage() {
  echo "Використання: $0 <команда> [аргументи]"
  echo "Команди:"
  echo "  add <slug> --pdf <path> --title <title> --authors <authors> --lang <lang>"
  echo "  run <slug> [--clean] [--no-translate] [--no-ebook] [--no-audio]"
  echo "  status <slug>"
  echo "  serve [--port <port>] [--dev]"
  exit 1
}

if [ $# -lt 1 ]; then
  show_usage
fi

ACTION="$1"
case "$ACTION" in
  add)
    SLUG="$2"
    if [ -z "$SLUG" ]; then
      echo "Помилка: Відсутній ідентифікатор книги (slug)" >&2
      show_usage
    fi
    validate_slug "$SLUG"
    
    shift 2
    PDF_PATH=""
    TITLE=""
    AUTHORS=""
    LANG=""
    SOURCE_LANG="ru"
    IS_MANGA=0
    
    while [ $# -gt 0 ]; do
      case "$1" in
        --pdf)
          if [ -n "$2" ]; then
            PDF_PATH="$2"
            shift 2
          else
            echo "Error: --pdf requires a path argument" >&2
            exit 1
          fi
          ;;
        --title)
          if [ -n "$2" ]; then
            TITLE="$2"
            shift 2
          else
            echo "Error: --title requires a title argument" >&2
            exit 1
          fi
          ;;
        --authors)
          if [ -n "$2" ]; then
            AUTHORS="$2"
            shift 2
          else
            echo "Error: --authors requires an author argument" >&2
            exit 1
          fi
          ;;
        --lang)
          if [ -n "$2" ]; then
            LANG="$2"
            shift 2
          else
            echo "Error: --lang requires a language argument" >&2
            exit 1
          fi
          ;;
        --source-lang)
          if [ -n "$2" ]; then
            SOURCE_LANG="$2"
            shift 2
          else
            echo "Error: --source-lang requires a language argument" >&2
            exit 1
          fi
          ;;
        --manga)
          IS_MANGA=1
          shift
          ;;
        *)
          echo "Error: Unknown argument '$1' for add command" >&2
          exit 1
          ;;
      esac
    done
    
    if [ -z "$PDF_PATH" ] || [ -z "$TITLE" ] || [ -z "$AUTHORS" ] || [ -z "$LANG" ]; then
      echo "Error: All options --pdf, --title, --authors, and --lang are required." >&2
      exit 1
    fi
    
    # Run the add_book python logic securely using environment variables
    SLUG="$SLUG" PDF_PATH="$PDF_PATH" TITLE="$TITLE" AUTHORS="$AUTHORS" LANG="$LANG" SOURCE_LANG="$SOURCE_LANG" IS_MANGA="$IS_MANGA" \
    python3 -c "
import os, sys
from kbg_web.status_helper import add_book
try:
    add_book(
        os.environ['SLUG'],
        os.environ['PDF_PATH'],
        os.environ['TITLE'],
        os.environ['AUTHORS'],
        os.environ['LANG'],
        os.environ.get('SOURCE_LANG', 'ru'),
        os.environ.get('IS_MANGA', '0') == '1'
    )
except Exception as e:
    print(f'Error: {e}', file=sys.stderr)
    sys.exit(1)
"
    ;;

  run)
    SLUG="$2"
    if [ -z "$SLUG" ]; then
      echo "Error: Missing book slug" >&2
      show_usage
    fi
    validate_slug "$SLUG"
    
    # Parse run flags
    shift 2
    CLEAN=0
    NO_TRANSLATE=0
    NO_EBOOK=0
    NO_AUDIO=0
    
    run_args=()
    while [ $# -gt 0 ]; do
      case "$1" in
        --clean)
          CLEAN=1
          run_args+=("--clean")
          shift
          ;;
        --no-translate)
          NO_TRANSLATE=1
          run_args+=("--no-translate")
          shift
          ;;
        --no-ebook)
          NO_EBOOK=1
          run_args+=("--no-ebook")
          shift
          ;;
        --no-audio)
          NO_AUDIO=1
          run_args+=("--no-audio")
          shift
          ;;
        *)
          echo "Error: Unknown argument '$1' for run command" >&2
          exit 1
          ;;
      esac
    done
    
    # Determine if translation server is needed
    IS_TRANSLATION_NEEDED=$(SLUG="$SLUG" python3 -c "
import json, os, sys
slug = os.environ['SLUG']
config_path = f'books/{slug}/config.json'
if not os.path.exists(config_path):
    print('false')
    sys.exit(0)
try:
    with open(config_path, 'r', encoding='utf-8') as f:
        cfg = json.load(f)
    source = cfg.get('source_lang', 'ru')
    target = cfg.get('target_lang', 'uk')
    print('true' if source != target else 'false')
except Exception:
    print('false')
")

    if [ "$IS_TRANSLATION_NEEDED" = "true" ] && [ "$NO_TRANSLATE" -eq 0 ]; then
      # Verify translation server connectivity
      if ! LD_LIBRARY_PATH="" curl -s --connect-timeout 2 "http://127.0.0.1:8081" >/dev/null; then
        echo "Сервер перекладу не запущено на порту 8081. Спроба запуску..."
        if [ -f "$HOME/start-translation-server.sh" ]; then
          bash "$HOME/start-translation-server.sh"
          echo -n "Очікування готовності сервера перекладу..."
          for i in {1..15}; do
            if LD_LIBRARY_PATH="" curl -s --connect-timeout 1 "http://127.0.0.1:8081" >/dev/null; then
              echo " Підключено!"
              break
            fi
            echo -n "."
            sleep 1
          done
          if ! LD_LIBRARY_PATH="" curl -s --connect-timeout 1 "http://127.0.0.1:8081" >/dev/null; then
            echo " Помилка: Сервер перекладу не зміг стартувати на порту 8081." >&2
            exit 1
          fi
        else
          echo "Помилка: Скрипт запуску сервера перекладу (~/start-translation-server.sh) не знайдено." >&2
          exit 1
        fi
      else
        echo "Сервер перекладу вже працює на порту 8081."
      fi
    fi
    
    # Run the orchestrator with safe argument passing
    python3 run_conversion_batches.py --book "$SLUG" "${run_args[@]}"
    ;;

  status)
    SLUG="$2"
    if [ -z "$SLUG" ]; then
      echo "Error: Missing book slug" >&2
      show_usage
    fi
    validate_slug "$SLUG"
    
    # Call the python helper to read caches and calculate progress percentages
    SLUG="$SLUG" PYTHONPATH=. python3 -c "
import os
from kbg_web.status_helper import print_status
print_status(os.environ['SLUG'])
"
    ;;

  serve)
    PORT=5000
    DEV=0
    shift
    while [ $# -gt 0 ]; do
      case "$1" in
        --port)
          if [ -n "$2" ] && [[ "$2" =~ ^[0-9]+$ ]]; then
            PORT="$2"
            shift 2
          else
            echo "Error: --port requires a numeric argument" >&2
            exit 1
          fi
          ;;
        --dev)
          DEV=1
          shift
          ;;
        *)
          echo "Error: Unknown argument '$1' for serve command" >&2
          exit 1
          ;;
      esac
    done
    
    if [ "$DEV" -eq 1 ]; then
      python3 kbg_web/app.py --port "$PORT" --debug
    else
      python3 kbg_web/app.py --port "$PORT"
    fi
    ;;

  *)
    echo "Error: Unknown action '$ACTION'" >&2
    show_usage
    ;;
esac
