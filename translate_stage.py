#!/usr/bin/env python3
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

import re
import json
import hashlib
import argparse
import requests
from common.text_protect import PlaceholderManager
from common.book_paths import resolve_book_paths
from common.utils import get_hash, split_into_segments, to_xml_format, wait_for_server_ready, translate_segment_with_retry
from common.file_lock import file_lock

def log(message):
    print(f"[Translate] {message}", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Markdown translation module via llama-server")
    parser.add_argument("--input", "-i", required=False, help="Input Markdown file")
    parser.add_argument("--output", "-o", required=False, help="Output Markdown file")
    parser.add_argument("--api-url", default="http://localhost:8081/v1/chat/completions", help="Llama server API endpoint")
    parser.add_argument("--cache", default=None, help="Cache file for progress tracking")
    parser.add_argument("--book", "-b", help="Book slug")
    parser.add_argument("--config", "-c", help="Book configuration JSON path")
    parser.add_argument("--target-lang", "-t", default="uk", help="Target language (default: uk)")
    args = parser.parse_args()
    
    if not args.input and not args.book and not args.config:
        parser.error("At least one of --input, --book, or --config must be specified.")
        
    repo_dir = os.path.dirname(os.path.abspath(__file__))
    
    slug = args.book
    if not slug and args.config:
        try:
            with open(args.config, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                slug = cfg.get("slug")
        except Exception:
            pass
    if not slug:
        slug = "default-book"
        
    paths = resolve_book_paths(repo_dir, slug, config_path=args.config)
    
    input_path = args.input
    if not input_path:
        input_path = os.path.join(paths["book_dir"], "input", "input.md")
        
    output_path = args.output
    if not output_path:
        output_path = os.path.join(paths["output_dir"], "output.md")
        
    cache_path = args.cache
    if not cache_path:
        if args.book or args.config:
            cache_path = paths["translate_cache"]
        else:
            cache_path = "progress_translate.json"
            
    target_lang = args.target_lang
    if paths.get("target_lang"):
        target_lang = paths["target_lang"]
        
    # Ensure directories exist
    os.makedirs(os.path.dirname(os.path.abspath(input_path)), exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(cache_path)), exist_ok=True)
    
    if not os.path.exists(input_path):
        log(f"Помилка: Вихідний файл '{input_path}' не існує.")
        sys.exit(1)
        
    # Check server availability and wait for model loading
    if not wait_for_server_ready(args.api_url):
        log("Помилка: Сервер перекладу не готовий до роботи.")
        log("Будь ласка, запустіть сервер перекладу за допомогою:")
        log("llama-server -m ~/models/hy-mt2/Hy-MT2-7B-Q4_K_M.gguf -c 4096 -t 4 --port 8081")
        sys.exit(1)
        
    log(f"Читання вихідного файлу: {input_path}")
    with open(input_path, "r", encoding="utf-8") as f:
        source_text = f.read()
        
    log("Захист елементів Markdown заповнювачами (placeholders)...")
    pm = PlaceholderManager()
    protected_text = pm.protect(source_text)
    
    log("Розбиття тексту на логічні сегменти...")
    segments = split_into_segments(protected_text)
    log(f"Усього сегментів для перекладу: {len(segments)}")
    
    # Load cache
    cache = {}
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as cf:
                cache = json.load(cf)
            log(f"Завантажено кеш з {cache_path} ({len(cache)} записів).")
        except Exception as e:
            log(f"Попередження: Не вдалося завантажити кеш: {e}. Початок з нуля.")
            
    translated_segments = []
    
    for idx, seg in enumerate(segments):
        seg_hash = get_hash(seg)
        if seg_hash in cache:
            translated_segments.append(cache[seg_hash])
        else:
            log(f"Переклад сегменту {idx+1}/{len(segments)} (довжина: {len(seg)} симв.)...")
            translated_seg = translate_segment_with_retry(seg, pm, args.api_url, target_lang=target_lang, source_lang=paths["source_lang"], book_dir=paths["book_dir"])
            if not translated_seg:
                raise ValueError(f"Критична помилка: Не вдалося перекласти сегмент {idx+1} після всіх спроб.")
            translated_segments.append(translated_seg)
            # Save to cache
            cache[seg_hash] = translated_seg
            try:
                with open(cache_path, "w", encoding="utf-8") as cf:
                    json.dump(cache, cf, ensure_ascii=False, indent=2)
            except Exception as e:
                log(f"Попередження: Не вдалося зберегти кеш: {e}")
                
    log("Об'єднання перекладених сегментів...")
    translated_protected_text = "\n\n".join(translated_segments)
    
    log("Відновлення оригінальних елементів Markdown...")
    final_text = pm.restore(translated_protected_text)
    
    # Write to output. Locked because a live edit (TASK-23) may concurrently
    # patch this same per-batch file while the main pipeline is still running.
    with file_lock(output_path):
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(final_text)
    log(f"Переклад успішно завершено! Збережено у: {output_path}")

if __name__ == "__main__":
    main()
