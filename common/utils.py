import hashlib
import os
import re
import requests
import time
import sys

def get_hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()

def split_into_segments(text, max_chars=1200):
    paragraphs = text.split("\n\n")
    segments = []
    current_segment = []
    current_length = 0
    
    for p in paragraphs:
        p_len = len(p)
        if p_len > max_chars:
            if current_segment:
                segments.append("\n\n".join(current_segment))
                current_segment = []
                current_length = 0
            # Split large paragraph by sentences
            sentences = re.split(r'(?<=[.!?])\s+', p)
            curr_sent_group = []
            curr_sent_len = 0
            for s in sentences:
                if curr_sent_len + len(s) > max_chars:
                    if curr_sent_group:
                        segments.append(" ".join(curr_sent_group))
                    curr_sent_group = [s]
                    curr_sent_len = len(s)
                else:
                    curr_sent_group.append(s)
                    curr_sent_len += len(s) + 1
            if curr_sent_group:
                segments.append(" ".join(curr_sent_group))
        else:
            if current_length + p_len > max_chars:
                segments.append("\n\n".join(current_segment))
                current_segment = [p]
                current_length = p_len
            else:
                current_segment.append(p)
                current_length += p_len + 2
                
    if current_segment:
        segments.append("\n\n".join(current_segment))
        
    return segments

def to_xml_format(text):
    prefix_map = {
        "IMAGE_LINE": "img",
        "CODE_BLOCK": "code",
        "MATH_BLOCK": "math",
        "MATH_INLINE": "mi",
        "INLINE_CODE": "ic",
        "LINK_URL": "link",
        "RAW_URL": "url",
        "HTML_TAG": "tag"
    }
    def repl(match):
        prefix = match.group(1)
        num = match.group(2)
        short = prefix_map.get(prefix, "t")
        return f"[{short}{num}]"
    return re.sub(r"__([A-Z_]+?)_(\d+)__", repl, text)

def to_prefix_format(text, pm):
    def repl(match):
        num = match.group(2)
        suffix = f"_{num}__"
        for key in pm.placeholders.keys():
            if key.endswith(suffix):
                return key
        return match.group(0)
    return re.sub(r"\[\s*([a-zA-Z]+)[-_\s]*(\d+)\s*\]", repl, text)

def wait_for_server_ready(api_url, max_wait=300, wait_interval=5):
    test_url = api_url.replace("/chat/completions", "").replace("/completion", "").rstrip("/")
    health_url = f"{test_url}/health"
    
    print(f"[Translation] Checking connection to server at {test_url}...", flush=True)
    for attempt in range(max_wait // wait_interval):
        try:
            res = requests.get(health_url, timeout=5)
            if res.status_code == 200:
                print(f"[Translation] Connected to translation server at {test_url} (ready)", flush=True)
                return True
            elif res.status_code == 503:
                print(f"[Translation] Translation server is loading the model (503)... waiting {wait_interval}s...", flush=True)
            else:
                print(f"[Translation] Translation server returned status {res.status_code}... waiting {wait_interval}s...", flush=True)
        except Exception as e:
            print(f"[Translation] Waiting for translation server to start/recover: {e}", flush=True)
        time.sleep(wait_interval)
    return False

def _is_hy_mt2_model(api_url):
    base = api_url.replace("/v1/chat/completions", "").replace("/completion", "").rstrip("/")
    # Try props endpoint first
    try:
        props_resp = requests.get(f"{base}/props", timeout=5)
        if props_resp.status_code == 200:
            props = props_resp.json()
            model_path = str(props.get("model_path", "")) or str(props.get("model_alias", ""))
            if "hy-mt2" in model_path.lower() or "hy_mt" in model_path.lower():
                return True
    except Exception:
        pass
    # Fallback to slots
    try:
        slot_resp = requests.get(f"{base}/slots", timeout=5)
        if slot_resp.status_code == 200:
            slots = slot_resp.json()
            if slots and isinstance(slots, list):
                model_path = str(slots[0].get("model", ""))
                return "hy-mt2" in model_path.lower() or "hy_mt" in model_path.lower()
    except Exception:
        pass
    return False

def clean_translation_text(raw):
    if not raw:
        return raw
    # Remove all closed tone_analysis tags (and trailing spaces/newlines)
    cleaned = re.sub(r'<tone_analysis>.*?</tone_analysis>\s*', '', raw, flags=re.DOTALL)
    # Handle unclosed tags (e.g. <tone_analysis>suspense\nTranslation)
    # Match <tone_analysis> followed by at most 100 characters (excluding '<' or '\n') and then a newline or end of string.
    cleaned = re.sub(r'<tone_analysis>[^<\n]{0,100}(?:\n|\Z)\s*', '', cleaned)
    # Clean up any leftover '<tone_analysis>' or '</tone_analysis>' tag just in case
    cleaned = re.sub(r'<tone_analysis>\s*', '', cleaned)
    cleaned = re.sub(r'</tone_analysis>\s*', '', cleaned)
    return cleaned.strip()

def translate_text_hy_mt2(text, base_url, source_lang="ru", target_lang="uk", temperature=0.1, cast_rules=None):
    lang_map = {
        "uk": "Ukrainian",
        "ru": "Russian",
        "en": "English"
    }
    source_lang_full = lang_map.get(source_lang, "Russian")
    target_lang_full = lang_map.get(target_lang, "Ukrainian")

    base = base_url.replace("/v1/chat/completions", "").replace("/completion", "").rstrip("/")
    is_7b_format = False
    
    try:
        props = requests.get(f"{base}/props", timeout=15).json()
        tmpl = props.get("chat_template", "") or props.get("model_alias", "") or props.get("model_path", "")
        is_7b_format = "startoftext" in tmpl or "extra_0" in tmpl or "7b" in tmpl.lower()
        print(f"[Translation] Format detection: is_7b_format={is_7b_format} (derived from template/alias/path)", flush=True)
    except Exception as e:
        print(f"[Translation] Format detection warning: failed to fetch props: {e}. Defaulting to 1.8B format.", flush=True)

    rules_prefix = f"{cast_rules}\n\n" if cast_rules else ""
    if is_7b_format:
        raw_prompt = (
            f"<|startoftext|>Translate the following text from {source_lang_full} to {target_lang_full}. "
            f"First, in a single <tone_analysis> tag, briefly state the emotional register of this passage (neutral/aggressive/melancholic/suspense) in a few words. "
            f"Then, after closing the tag, output ONLY the translation with no further explanation or commentary:\n\n{rules_prefix}{text}<|extra_0|>"
        )
        stop_tokens = ["<|eos|>", "<|startoftext|>", "<|extra_0|>"]
    else:
        raw_prompt = (
            f"<|hy_begin\u2581of\u2581sentence|>"
            f"<|hy_User|>Translate the following text from {source_lang_full} to {target_lang_full}. "
            f"First, in a single <tone_analysis> tag, briefly state the emotional register of this passage (neutral/aggressive/melancholic/suspense) in a few words. "
            f"Then, after closing the tag, output ONLY the translation with no further explanation or commentary:\n\n{rules_prefix}{text}<|hy_Assistant|>"
        )
        stop_tokens = ["<|hy_User|>", "<|hy_begin\u2581of\u2581sentence|>", "<|endoftext|>"]

    completion_url = base_url.replace("/v1/chat/completions", "/completion").rstrip("/")
    if not completion_url.endswith("/completion"):
        completion_url = completion_url.rstrip("/") + "/completion"

    headers = {"Content-Type": "application/json"}
    data = {
        "prompt": raw_prompt,
        "temperature": temperature,
        "top_p": 0.95,
        "top_k": 20,
        "repeat_penalty": 1.05,
        "n_predict": 4096,
        "stop": stop_tokens
    }

    while True:
        try:
            resp = requests.post(completion_url, headers=headers, json=data, timeout=600)
            if resp.status_code == 503:
                print("[Translation] Server returned 503 (model loading). Waiting...", flush=True)
                wait_for_server_ready(base_url)
                continue
            if resp.status_code != 200:
                print(f"[Translation] Hy-MT2 /completion error: {resp.status_code} - {resp.text[:200]}", flush=True)
                return None
            result = resp.json()
            translated = result.get("content", "").strip()
            cleaned = clean_translation_text(translated)
            return cleaned if cleaned else None
        except Exception as e:
            print(f"[Translation] Hy-MT2 API request failed: {e}. Checking server status...", flush=True)
            wait_for_server_ready(base_url)

def translate_text(text, api_url, target_lang="uk", temperature=0.7, source_lang="ru", cast_rules=None):
    if not wait_for_server_ready(api_url):
        raise ConnectionError(f"Translation server at {api_url} is not reachable.")

    if _is_hy_mt2_model(api_url):
        print("[Translation] Detected Hy-MT2 model — using raw /completion endpoint", flush=True)
        return translate_text_hy_mt2(text, api_url, source_lang=source_lang,
                                     target_lang=target_lang, temperature=0.1, cast_rules=cast_rules)

    lang_map = {
        "uk": "Ukrainian",
        "ru": "Russian",
        "en": "English"
    }
    target_lang_full = lang_map.get(target_lang, "Ukrainian")
    prompt = f"Translate the following text into {target_lang_full}:\n\n"
    if cast_rules:
        prompt += f"{cast_rules}\n\n"
    prompt += text
    headers = {"Content-Type": "application/json"}
    data = {
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "top_p": 0.6,
        "top_k": 20,
        "repetition_penalty": 1.05,
        "max_tokens": 4096
    }
    
    while True:
        try:
            response = requests.post(api_url, headers=headers, json=data, timeout=600)
            if response.status_code == 503:
                print("[Translation] Server returned 503 (model loading). Waiting...", flush=True)
                wait_for_server_ready(api_url)
                continue
            if response.status_code != 200:
                print(f"[Translation] Error from llama-server: {response.status_code} - {response.text}", flush=True)
                return None
            res_json = response.json()
            translated = res_json["choices"][0]["message"]["content"].strip()
            
            clean_prefixes = [
                f"here is the translation into {target_lang_full.lower()}:",
                f"переклад {target_lang_full.lower()}ською:",
                "translation:",
                "ось переклад:"
            ]
            for pref in clean_prefixes:
                if translated.lower().startswith(pref):
                    translated = translated[len(pref):].strip()
            return translated
        except Exception as e:
            print(f"[Translation] API request failed: {e}. Checking server status...", flush=True)
            wait_for_server_ready(api_url)

def validate_translation_segment(original, translated):
    if not translated:
        return False

    orig_headers = len([line for line in original.splitlines() if line.strip().startswith('#')])
    if orig_headers > 0:
        trans_headers = len([line for line in translated.splitlines() if line.strip().startswith('#')])
        if orig_headers != trans_headers:
            print(f"[Validation] failure: Headers count mismatch! Original: {orig_headers}, Translated: {trans_headers}", flush=True)
            return False
        
    orig_placeholders = set(re.findall(r"__[A-Z_]+_[0-9]+__", original))
    trans_placeholders = set(re.findall(r"__[A-Z_]+_[0-9]+__", translated))
    
    missing = orig_placeholders - trans_placeholders
    extra = trans_placeholders - orig_placeholders
    
    if missing or extra:
        print("[Validation] failure: Placeholders mismatch!", flush=True)
        print(f"  Original segment: {original!r}", flush=True)
        print(f"  Translated segment: {translated!r}", flush=True)
        if missing:
            print(f"  Missing in translation: {missing}", flush=True)
        if extra:
            print(f"  Extra in translation: {extra}", flush=True)
        return False
    return True

def _maybe_mqm_review(segment, result, api_url, source_lang, target_lang, book_dir):
    """Best-effort MQM quality review, opt-in via config.json's
    enable_mqm_review (default False - roughly doubles per-segment LLM
    time, so it must never silently turn on for existing books/users).
    Never raises, never mutates the translation - purely a side-channel
    write to translation_quality_flags.json for later human review."""
    if not book_dir or not result:
        return
    try:
        import json as _json
        cfg_path = os.path.join(book_dir, "config.json")
        with open(cfg_path, "r", encoding="utf-8") as f:
            if not (_json.load(f) or {}).get("enable_mqm_review"):
                return
    except Exception:
        return
    try:
        from common.mqm_review import review_and_record
        segment_id = hashlib.sha256(segment.encode("utf-8")).hexdigest()[:16]
        flags_path = os.path.join(book_dir, "translation_quality_flags.json")
        review_and_record(
            segment_id=segment_id,
            original=segment,
            translated=result,
            api_url=api_url,
            flags_path=flags_path,
            source_lang=source_lang,
            target_lang=target_lang,
        )
    except Exception as e:
        print(f"[Translation] Warning: MQM review failed (non-blocking): {e}", flush=True)


def translate_segment_with_retry(segment, pm, api_url, target_lang="uk", max_retries=3, source_lang="ru", book_dir=None):
    temp = 0.7
    xml_segment = to_xml_format(segment)
    orig_placeholders = set(re.findall(r"__[A-Z_]+_[0-9]+__", segment))
    last_translated = None

    # Try to load cast registry if book_dir is provided
    cast_rules = ""
    if book_dir:
        try:
            from common.cast_registry import registry_enabled, load_characters, cast_rules_block
            if registry_enabled(book_dir):
                chars = load_characters(book_dir)
                if chars:
                    cast_rules = cast_rules_block(chars, segment)
                    if cast_rules:
                        print(f"[Translation] Injected cast registry rules for segment:\n{cast_rules}", flush=True)
        except Exception as e:
            print(f"[Translation] Warning: Failed to generate cast rules: {e}", flush=True)

    for attempt in range(max_retries):
        if attempt > 0:
            temp = 0.1
            print(f"[Translation] Retrying segment (attempt {attempt+1}/{max_retries}) with temperature {temp}...", flush=True)

        translated = translate_text(xml_segment, api_url, target_lang=target_lang, temperature=temp, source_lang=source_lang, cast_rules=cast_rules)
        if not translated:
            continue

        translated = to_prefix_format(translated, pm)
        translated = pm.normalize_placeholders(translated)

        last_translated = translated

        if validate_translation_segment(segment, translated):
            _maybe_mqm_review(segment, translated, api_url, source_lang, target_lang, book_dir)
            return translated
        else:
            print(f"[Translation] Segment validation failed on attempt {attempt+1}.", flush=True)

    # If all attempts failed, rescue by adjusting missing/extra placeholders
    if last_translated:
        trans_placeholders = set(re.findall(r"__[A-Z_]+_[0-9]+__", last_translated))
        missing = orig_placeholders - trans_placeholders
        extra = trans_placeholders - orig_placeholders

        rescued = last_translated
        if extra:
            print(f"[Translation] Stripping extra placeholders from translation: {extra}", flush=True)
            for ex in extra:
                rescued = rescued.replace(ex, "")

        if missing:
            print(f"[Translation] Appending missing placeholders to translation: {missing}", flush=True)
            rescued = rescued + " " + " ".join(sorted(list(missing)))

        if validate_translation_segment(segment, rescued):
            _maybe_mqm_review(segment, rescued, api_url, source_lang, target_lang, book_dir)
            return rescued

    print("[Translation] Error: Segment validation failed after all retries. Translation failed.", flush=True)
    return None
