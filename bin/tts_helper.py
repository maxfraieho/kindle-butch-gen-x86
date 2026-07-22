#!/usr/bin/env python3
import sys
import os
import json
import re
import subprocess
import wave
import struct

_repo_dir = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
if _repo_dir not in sys.path:
    sys.path.insert(0, _repo_dir)
from common.file_lock import file_lock, LockTimeoutError
from common.heartbeat import send_heartbeat


def _splice_audio_priority(chunks, seen_hashes, audio_priority_path):
    """TASK-23: called once per chunk from inside the already-running,
    already-model-loaded tts_helper.py loop, to pick up new live-edit
    entries kbg_web/app.py queued (via edit_regenerate_audio) while this
    book's audio_stage.py run is active - avoids spinning up a second
    model-loaded process for a mid-run edit (the confirmed pre-TASK-23
    resource-conflict race)."""
    if not audio_priority_path or not os.path.exists(audio_priority_path):
        return
    try:
        with file_lock(audio_priority_path, timeout=0.5):
            with open(audio_priority_path, "r", encoding="utf-8") as f:
                queued = json.load(f)
            new_entries = [q for q in queued if q.get("hash") not in seen_hashes]
            if new_entries:
                for q in new_entries:
                    chunks.append({"hash": q["hash"], "text": q["text"]})
                    seen_hashes.add(q["hash"])
                # Clear now that these entries are picked up - app.py will
                # create a fresh file if another live edit lands later.
                with open(audio_priority_path, "w", encoding="utf-8") as f:
                    json.dump([], f)
                print(f"[TTSHelper] Picked up {len(new_entries)} live-edit chunk(s) from priority queue.", flush=True)
    except LockTimeoutError:
        pass  # app.py is mid-write to the queue file; retry next chunk iteration
    except Exception:
        pass


def normalize_accents(text):
    # Convert spacing acute accent (´, \u00b4) to combining acute accent (́, \u0301)
    return text.replace("\u00b4", "\u0301")

def transliterate_english_words(text):
    # Dictionary of common technical terms with pronunciation and stress
    DICTIONARY = {
        r"\bVibe\b": "ва+йб", r"\bvibe\b": "ва+йб",
        r"\bAI\b": "е+й-а+й", r"\bai\b": "е+й-а+й",
        r"\bAPI\b": "е+й-пі-а+й", r"\bapi\b": "е+й-пі-а+й",
        r"\bGPU\b": "джи-пі-ю+", r"\bgpu\b": "джи-пі-ю+",
        r"\bCPU\b": "сі-пі-ю+", r"\bcpu\b": "сі-пі-ю+",
        r"\bNPU\b": "ен-пі-ю+", r"\bnpu\b": "ен-пі-ю+",
        r"\bDSP\b": "ді-ес-пі+", r"\bdsp\b": "ді-ес-пі+",
        r"\bONNX\b": "о+ннікс", r"\bonnx\b": "о+ннікс",
        r"\bStyleTTS2\b": "ста+йл-ті-ті-е+с-два", r"\bstyletts2\b": "ста+йл-ті-ті-е+с-два",
        r"\bTTS\b": "ті-ті-е+с", r"\btts\b": "ті-ті-е+с",
        r"\bFFmpeg\b": "еф-еф-е+мпег", r"\bffmpeg\b": "еф-еф-е+мпег",
        r"\bPython\b": "па+йтон", r"\bpython\b": "па+йтон",
        r"\bFastAPI\b": "фа+ст-е+й-пі-а+й", r"\bfastapi\b": "фа+ст-е+й-пі-а+й",
        r"\bPrompt\b": "промпт", r"\bprompt\b": "промпт",
        r"\bTermux\b": "те+рмукс", r"\btermux\b": "те+рмукс",
        r"\bLLM\b": "ел-ел-е+м", r"\bllm\b": "ел-ел-е+м",
        r"\bGPT\b": "джи-пі-ті+", r"\bgpt\b": "джи-пі-ті+",
        r"\bGGUF\b": "джі-джі-ю-е+ф", r"\bgguf\b": "джі-джі-ю-е+ф",
        r"\bGitHub\b": "гіт-ха+б", r"\bgithub\b": "гіт-ха+б",
        r"\bGit\b": "гіт", r"\bgit\b": "гіт",
        r"\bSSE\b": "ес-ес-і+", r"\bsse\b": "ес-ес-і+",
        r"\bHTTP\b": "е+йч-ті-ті-пі", r"\bhttp\b": "е+йч-ті-ті-пі",
        r"\bFastMCP\b": "фа+ст-ем-сі-пі+", r"\bfastmcp\b": "фа+ст-ем-сі-пі+",
        r"\bMCP\b": "ем-сі-пі+", r"\bmcp\b": "ем-сі-пі+",
        r"\bSDK\b": "ес-ді-ке+й", r"\bsdk\b": "ес-ді-ке+й",
        r"\bUI\b": "ю-а+й", r"\bui\b": "ю-а+й",
        r"\bUX\b": "ю-е+кс", r"\bux\b": "ю-е+кс",
        r"\bJSON\b": "дже+йсон", r"\bjson\b": "дже+йсон",
        r"\bSQL\b": "ес-кю-е+л", r"\bsql\b": "ес-кю-е+л",
        r"\bSQLite\b": "ес-кю-ла+йт", r"\bsqlite\b": "ес-кю-ла+йт",
        r"\bDocker\b": "до+кер", r"\bdocker\b": "до+кер",
        r"\bAndroid\b": "андро+їд", r"\bandroid\b": "андро+їд",
        r"\bRuntime\b": "ранта+йм", r"\bruntime\b": "ранта+йм",
        r"\bClaude\b": "кло+д", r"\bclaude\b": "кло+д",
        r"\bCodex\b": "ко+декс", r"\bcodex\b": "ко+декс",
        r"\bNode\b": "но+да", r"\bnode\b": "но+да",
        r"\bVercel\b": "версе+л", r"\bvercel\b": "версе+л",
        r"\bNext.js\b": "не+кст-дже-е+с", r"\bnext.js\b": "не+кст-дже-е+с",
        r"\bVite\b": "ва+йт", r"\bvite\b": "ва+йт",
        r"\bTailwind\b": "тейлві+нд", r"\btailwind\b": "тейлві+нд",
        r"\bHTML\b": "е+йч-ті-ем-е+л", r"\bhtml\b": "е+йч-ті-ем-е+л",
        r"\bCSS\b": "сі-ес-е+с", r"\bcss\b": "сі-ес-е+с",
        r"\bJS\b": "дже-е+с", r"\bjs\b": "дже-е+с",
        r"\bTS\b": "ті-е+с", r"\bts\b": "ті-е+с",
        r"\bCLI\b": "сі-ел-а+й", r"\bcli\b": "сі-ел-а+й",
    }
    
    # Apply direct dictionary replacements
    for pattern, replacement in DICTIONARY.items():
        text = re.sub(pattern, replacement, text)
        
    # Also handle letter-by-letter fallback for any remaining English words
    def repl(match):
        word = match.group(0)
        if word.isupper():
            LETTERS = {
                'A': 'а+', 'B': 'бе+', 'C': 'це+', 'D': 'де+', 'E': 'е+', 'F': 'еф', 'G': 'же+', 'H': 'аш', 
                'I': 'і+', 'J': 'жи+', 'K': 'ка+', 'L': 'ел', 'M': 'ем', 'N': 'ен', 'O': 'о+', 'P': 'пе+', 
                'Q': 'ку+', 'R': 'ер', 'S': 'ес', 'T': 'те+', 'U': 'ю+', 'V': 'ве+', 'W': 'дабл-ю', 
                'X': 'ікс', 'Y': 'ігре+к', 'Z': 'зет'
            }
            return "-".join([LETTERS.get(c, c) for c in word])
        else:
            word = word.lower()
            rules = [
                ('ch', 'ч'), ('sh', 'ш'), ('th', 'т'), ('ph', 'ф'), ('kh', 'х'), ('oo', 'у'), ('ee', 'і'),
                ('ck', 'к'), ('qu', 'кв'), ('ya', 'я'), ('ye', 'є'), ('yu', 'ю'), ('yo', 'йо'),
                ('a', 'а'), ('b', 'б'), ('c', 'к'), ('d', 'д'), ('e', 'е'), ('f', 'ф'), ('g', 'г'), 
                ('h', 'г'), ('i', 'і'), ('j', 'дж'), ('k', 'к'), ('l', 'л'), ('m', 'м'), ('n', 'н'), 
                ('o', 'о'), ('p', 'п'), ('q', 'к'), ('r', 'р'), ('s', 'с'), ('t', 'т'), ('u', 'у'), 
                ('v', 'в'), ('w', 'в'), ('x', 'кс'), ('y', 'й'), ('z', 'з')
            ]
            res = word
            for eng, ukr in rules:
                res = res.replace(eng, ukr)
            return res
            
    text = re.sub(r'[a-zA-Z]+', repl, text)
    return text

def trim_silence(samples, sample_rate, threshold=100, pad_start_ms=30, pad_end_ms=70):
    first_idx = None
    for i, s in enumerate(samples):
        if abs(s) > threshold:
            first_idx = i
            break
    last_idx = None
    for i in range(len(samples) - 1, -1, -1):
        if abs(samples[i]) > threshold:
            last_idx = i
            break
    if first_idx is None or last_idx is None:
        return samples
    pad_start = int((pad_start_ms / 1000.0) * sample_rate)
    pad_end = int((pad_end_ms / 1000.0) * sample_rate)
    start_idx = max(0, first_idx - pad_start)
    end_idx = min(len(samples), last_idx + pad_end + 1)
    return samples[start_idx:end_idx]

def run_supertonic3(payload):
    import sherpa_onnx

    output_dir = payload.get("output_dir")
    chunks = payload.get("chunks", [])
    speaker_id = payload.get("speaker_id", 2)
    speed = payload.get("speed", 1.0)
    lang = payload.get("lang", "uk")

    if not output_dir:
        print("[TTSHelper] Error: output_dir is required for Supertonic 3", file=sys.stderr)
        sys.exit(1)

    os.makedirs(output_dir, exist_ok=True)

    # Resolve model directory (default inside kindle-butch-gen/models)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    repo_dir = os.path.abspath(os.path.join(script_dir, ".."))
    model_dir = os.path.join(repo_dir, "models", "sherpa-onnx-supertonic-3-tts-int8-2026-05-11")

    if not os.path.exists(model_dir):
        print(f"[TTSHelper] Error: Supertonic 3 model directory not found at {model_dir}", file=sys.stderr)
        sys.exit(1)

    # Initialize OfflineTts
    tts_config = sherpa_onnx.OfflineTtsConfig(
        model=sherpa_onnx.OfflineTtsModelConfig(
            supertonic=sherpa_onnx.OfflineTtsSupertonicModelConfig(
                duration_predictor=os.path.join(model_dir, "duration_predictor.int8.onnx"),
                text_encoder=os.path.join(model_dir, "text_encoder.int8.onnx"),
                vector_estimator=os.path.join(model_dir, "vector_estimator.int8.onnx"),
                vocoder=os.path.join(model_dir, "vocoder.int8.onnx"),
                tts_json=os.path.join(model_dir, "tts.json"),
                unicode_indexer=os.path.join(model_dir, "unicode_indexer.bin"),
                voice_style=os.path.join(model_dir, "voice.bin"),
            ),
            debug=False,
            num_threads=4,
            provider="nnapi",
        )
    )

    if not tts_config.validate():
        print("[TTSHelper] Error: Supertonic 3 configuration validation failed", file=sys.stderr)
        sys.exit(1)

    tts = sherpa_onnx.OfflineTts(tts_config)

    gen_config = sherpa_onnx.GenerationConfig()
    gen_config.sid = int(speaker_id)
    gen_config.num_steps = 8
    gen_config.speed = float(speed)
    gen_config.extra["lang"] = lang

    # Load cache dynamically
    cache_path = payload.get("cache_path")
    if not cache_path:
        cache_path = os.path.join(os.path.dirname(output_dir), "tts_cache_supertonic-3-tts-int8.json")
    cache = {}
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:
            pass

    total = len(chunks)
    print(f"[TTSHelper] (Supertonic 3) Processing {total} chunks...", flush=True)

    audio_priority_path = payload.get("audio_priority_path")
    seen_hashes = {c.get("hash") for c in chunks}
    slug = payload.get("slug")

    for i, chunk in enumerate(chunks):
        chunk_hash = chunk.get("hash")
        text = chunk.get("text", "").strip()

        if not chunk_hash or not text:
            continue

        if slug:
            send_heartbeat(slug, f"{i + 1}/{total}", stage="озвучення")
        _splice_audio_priority(chunks, seen_hashes, audio_priority_path)

        # Transliterate English words and abbreviations
        text = transliterate_english_words(text)

        import unicodedata
        text = unicodedata.normalize("NFD", text)

        output_file = os.path.join(output_dir, f"{chunk_hash}.wav")

        if i < 5:
            print(f"[TTSHelper] [{i+1}/{total}] Synthesizing chunk {chunk_hash}:", flush=True)
            print(f"  - Cleaned text: '{text}'", flush=True)
        else:
            print(f"[TTSHelper] [{i+1}/{total}] Synthesizing chunk {chunk_hash}...", flush=True)

        try:
            audio = tts.generate(text, gen_config)
            if len(audio.samples) == 0:
                print(f"[TTSHelper] Error: Generated audio samples are empty for chunk {chunk_hash}", file=sys.stderr)
                continue

            # Decimate samples by 2 to downsample from 44100 Hz to 22050 Hz
            output_sample_rate = audio.sample_rate // 2
            int16_samples = [int(max(-1.0, min(1.0, s)) * 32767) for s in audio.samples[::2]]

            # Trim leading/trailing silence with custom end padding based on punctuation
            last_char = text[-1] if text else ""
            if last_char in [".", "!", "?", "…"]:
                custom_pad_end = 500  # 500 ms pause after sentences
            elif last_char in [",", ";", ":"]:
                custom_pad_end = 250  # 250 ms pause after clauses
            else:
                custom_pad_end = 250
                
            int16_samples = trim_silence(int16_samples, output_sample_rate, pad_end_ms=custom_pad_end)

            # Save wav file
            with wave.open(output_file, "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(output_sample_rate)
                packed_data = struct.pack(f"{len(int16_samples)}h", *int16_samples)
                wav_file.writeframes(packed_data)

            # Update cache file dynamically
            cache[chunk_hash] = text
            try:
                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump(cache, f, ensure_ascii=False, indent=2)
            except Exception:
                pass

        except Exception as e:
            print(f"[TTSHelper] Error running Supertonic 3 on chunk {chunk_hash}: {e}", file=sys.stderr)

def run_styletts2(payload):
    import onnxruntime
    import numpy as np
    try:
        from ipa_uk import ipa
    except ImportError:
        # ipa-uk vanished from PyPI (404, found by the first outside
        # install) - vendored copy shipped in the repo instead.
        import sys as _sys, os as _os
        _sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.dirname(
            _os.path.abspath(__file__))), "common", "vendor"))
        from ipa_uk import ipa

    output_dir = payload.get("output_dir")
    chunks = payload.get("chunks", [])
    speed = float(payload.get("speed", 1.0))
    voice_quality = payload.get("voice_quality", "x_low")
    noise_w = float(payload.get("noise_w", 0.8))
    noise_scale = float(payload.get("noise_scale", 0.667))

    if not output_dir:
        print("[TTSHelper] Error: output_dir is required for StyleTTS2", file=sys.stderr)
        sys.exit(1)

    os.makedirs(output_dir, exist_ok=True)

    script_dir = os.path.dirname(os.path.abspath(__file__))
    repo_dir = os.path.abspath(os.path.join(script_dir, ".."))
    model_path = os.path.join(repo_dir, "models", "styletts2", "model.onnx")
    style_path = os.path.join(repo_dir, "models", "styletts2", "style.npy")

    if not os.path.exists(model_path) or not os.path.exists(style_path):
        print(f"[TTSHelper] Error: StyleTTS2 model files not found at {model_path} or {style_path}", file=sys.stderr)
        sys.exit(1)

    # Vocabulary for tokenization
    VOCAB = [
        '$', '-', '´', ';', ':', ',', '.', '!', '?', '¡', '¿', '—', '…', '"', '«', '»', '“', '”', ' ', 
        '(', ')', '†', '/', '=', 'A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J', 'K', 'L', 'M', 'N', 
        'O', 'P', 'Q', 'R', 'S', 'T', 'U', 'V', 'W', 'X', 'Y', 'Z', 'a', 'b', 'c', 'd', 'e', 'f', 'g', 
        'h', 'i', 'j', 'k', 'l', 'm', 'n', 'o', 'p', 'q', 'r', 's', 't', 'u', 'v', 'w', 'x', 'y', 'z', 
        'é', 'ý', 'í', 'ó', "'", '̯', "'", '͡', 'ɑ', 'ɐ', 'ɒ', 'æ', 'ɓ', 'ʙ', 'β', 'ɔ', 'ɕ', 'ç', 'ɗ', 
        'ɖ', 'ð', 'ʤ', 'ə', 'ɘ', 'ɚ', 'ɛ', 'ɜ', 'ɝ', 'ɞ', 'ɟ', 'ʄ', 'ɡ', 'ɠ', 'ɢ', 'ʛ', 'ɦ', 'ɧ', 'ħ', 
        'ɥ', 'ʜ', 'ɨ', 'ɪ', 'ʝ', 'ɭ', 'ɬ', 'ɫ', 'ɮ', 'ʟ', 'ɱ', 'ɯ', 'ɰ', 'ŋ', 'ɳ', 'ɲ', 'ɴ', 'ø', 'ɵ', 
        'ɸ', 'θ', 'œ', 'ɶ', 'ʘ', 'ɹ', 'ɺ', 'ɾ', 'ɻ', 'ʀ', 'ʁ', 'ɽ', 'ʂ', 'ʃ', 'ʈ', 'ʧ', 'ʉ', 'ʊ', 'ʋ', 
        'ⱱ', 'ʌ', 'ɣ', 'ɤ', 'ʍ', 'χ', 'ʎ', 'ʏ', 'ʑ', 'ʐ', 'ʒ', 'ʔ', 'ʡ', 'ʕ', 'ʢ', 'ǀ', 'ǁ', 'ǂ', 'ǃ', 
        'ˈ', 'ˌ', 'ː', 'ˑ', 'ʼ', 'ʴ', 'ʰ', 'ʱ', 'ʲ', "'", '̩', "'", 'ᵻ'
    ]
    vocab_dict = {char: idx for idx, char in enumerate(VOCAB)}

    def get_token_count(text):
        cleaned = text.replace('+', '\u0301')
        ipa_text = ipa(cleaned)
        return len([c for c in ipa_text if c in vocab_dict])

    def split_long_text(text, max_tokens=400):
        if get_token_count(text) <= max_tokens:
            return [text]
        parts = re.split(r'([,.;!?\n]+)', text)
        sub_texts = []
        current = []
        for part in parts:
            temp = "".join(current) + part
            if get_token_count(temp) <= max_tokens:
                current.append(part)
            else:
                if current:
                    sub_texts.append("".join(current))
                current = [part]
        if current:
            sub_texts.append("".join(current))
        final_texts = []
        for st in sub_texts:
            if get_token_count(st) <= max_tokens:
                final_texts.append(st)
            else:
                words = st.split(" ")
                curr_words = []
                for w in words:
                    temp_w = " ".join(curr_words + [w])
                    if get_token_count(temp_w) <= max_tokens:
                        curr_words.append(w)
                    else:
                        if curr_words:
                            final_texts.append(" ".join(curr_words))
                        curr_words = [w]
                if curr_words:
                    final_texts.append(" ".join(curr_words))
        return final_texts

    # Initialize Session
    sess_options = onnxruntime.SessionOptions()
    sess = onnxruntime.InferenceSession(model_path, sess_options, providers=['NnapiExecutionProvider', 'CPUExecutionProvider'])
    s_prev = np.load(style_path).astype(np.float32)

    # Load cache dynamically
    cache_path = payload.get("cache_path")
    if not cache_path:
        cache_path = os.path.join(os.path.dirname(output_dir), "tts_cache_styletts2.json")
    cache = {}
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:
            pass

    total = len(chunks)
    print(f"[TTSHelper] (StyleTTS2) Processing {total} chunks...", flush=True)

    audio_priority_path = payload.get("audio_priority_path")
    seen_hashes = {c.get("hash") for c in chunks}
    slug = payload.get("slug")

    for i, chunk in enumerate(chunks):
        chunk_hash = chunk.get("hash")
        text = chunk.get("text", "").strip()

        if slug:
            send_heartbeat(slug, f"{i + 1}/{total}", stage="озвучення")
        if not chunk_hash or not text:
            continue

        _splice_audio_priority(chunks, seen_hashes, audio_priority_path)

        # Transliterate English words and abbreviations
        text = transliterate_english_words(text)

        # Check token count and split if needed
        sub_texts = split_long_text(text)
        
        chunk_audio_samples = []
        chunk_failed = False
        
        for sub_idx, sub_text in enumerate(sub_texts):
            # Replace '+' with Combining Acute Accent for proper phonemization
            cleaned_text = sub_text.replace('+', '\u0301')

            # Transcribe to IPA
            ipa_text = ipa(cleaned_text)
            
            # Tokenize
            indexes = []
            for char in ipa_text:
                if char in vocab_dict:
                    indexes.append(vocab_dict[char])
            tokens = np.array(indexes, dtype=np.int64)

            if len(tokens) == 0:
                continue

            if i < 5:
                print(f"[TTSHelper] [{i+1}/{total}] Synthesizing sub-chunk {sub_idx+1}/{len(sub_texts)} of chunk {chunk_hash}:", flush=True)
                print(f"  - Original sub-text: '{sub_text}'", flush=True)
                print(f"  - IPA: '{ipa_text}'", flush=True)
            elif sub_idx == 0:
                print(f"[TTSHelper] [{i+1}/{total}] Synthesizing chunk {chunk_hash} ({len(sub_texts)} parts)...", flush=True)

            try:
                # Map tts_voice_quality to diffusion_steps
                quality_map = {
                    "x_low": 5,
                    "low": 10,
                    "medium": 15,
                    "high": 20
                }
                diffusion_steps = quality_map.get(voice_quality.lower(), 10)

                # Map noise_w to t (0.6 - 1.0)
                clamped_noise_w = max(0.1, min(1.5, noise_w))
                t_val = 0.6 + (clamped_noise_w - 0.1) * (0.4 / 1.4)
                t_val = float(max(0.6, min(1.0, t_val)))

                # Map noise_w to embedding_scale (0.5 - 3.0)
                embedding_scale_val = float(max(0.1, min(3.0, noise_w)))

                # Map noise_scale to alpha/beta (0.0 to 1.0)
                clamped_noise_scale = max(0.1, min(1.5, noise_scale))
                alpha_val = 0.3 * (clamped_noise_scale / 0.667)
                alpha_val = float(max(0.0, min(1.0, alpha_val)))
                beta_val = 0.7 * (clamped_noise_scale / 0.667)
                beta_val = float(max(0.0, min(1.0, beta_val)))

                # Dynamically construct ONNX inputs
                session_inputs = [inp.name for inp in sess.get_inputs()]
                inputs = {}
                
                # Check for tokens
                if 'tokens' in session_inputs:
                    inputs['tokens'] = tokens
                elif 'input_ids' in session_inputs:
                    inputs['input_ids'] = tokens
                    
                # Check for speed
                if 'speed' in session_inputs:
                    inputs['speed'] = np.array(speed, dtype=np.float32)
                    
                # Check for s_prev/style
                if 's_prev' in session_inputs:
                    inputs['s_prev'] = s_prev
                elif 'style' in session_inputs:
                    inputs['style'] = s_prev
                    
                # Check for other style diffusion params
                if 'diffusion_steps' in session_inputs:
                    inputs['diffusion_steps'] = np.array(diffusion_steps, dtype=np.int64)
                if 't' in session_inputs:
                    inputs['t'] = np.array(t_val, dtype=np.float32)
                if 'embedding_scale' in session_inputs:
                    inputs['embedding_scale'] = np.array(embedding_scale_val, dtype=np.float32)
                if 'alpha' in session_inputs:
                    inputs['alpha'] = np.array(alpha_val, dtype=np.float32)
                if 'beta' in session_inputs:
                    inputs['beta'] = np.array(beta_val, dtype=np.float32)

                # Fallback to standard input signature if dynamically found inputs list is empty or doesn't match standard
                if not inputs:
                    inputs = {
                        'tokens': tokens,
                        'speed': np.array(speed, dtype=np.float32),
                        's_prev': s_prev
                    }

                outputs = sess.run(None, inputs)
                sub_audio = outputs[0]
                
                if len(sub_audio) == 0:
                    print(f"[TTSHelper] Error: Generated sub-audio samples are empty for chunk {chunk_hash} part {sub_idx+1}", file=sys.stderr)
                    chunk_failed = True
                    break
                
                chunk_audio_samples.append(sub_audio)
            except Exception as e:
                print(f"[TTSHelper] Error running StyleTTS2 on chunk {chunk_hash} part {sub_idx+1}: {e}", file=sys.stderr)
                chunk_failed = True
                break

        if chunk_failed or not chunk_audio_samples:
            # If failed, generate a silent WAV
            print(f"[TTSHelper] Warning: Generating silent WAV for failed/empty chunk {chunk_hash}", file=sys.stderr)
            output_file = os.path.join(output_dir, f"{chunk_hash}.wav")
            silence_samples = [0] * 2400
            try:
                with wave.open(output_file, "wb") as wav_file:
                    wav_file.setnchannels(1)
                    wav_file.setsampwidth(2)
                    wav_file.setframerate(24000)
                    packed_data = struct.pack(f"{len(silence_samples)}h", *silence_samples)
                    wav_file.writeframes(packed_data)
                cache[chunk_hash] = text
                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump(cache, f, ensure_ascii=False, indent=2)
            except Exception as ce:
                print(f"[TTSHelper] Error writing silent WAV: {ce}", file=sys.stderr)
            continue

        # Concatenate audio samples from all sub-chunks
        audio_samples = np.concatenate(chunk_audio_samples)
        output_file = os.path.join(output_dir, f"{chunk_hash}.wav")

        try:
            # Normalize samples to int16
            int16_samples = [int(max(-1.0, min(1.0, s)) * 32767) for s in audio_samples]

            # Trim leading/trailing silence (StyleTTS2 native sample rate is 24000 Hz) with custom end padding
            last_char = text[-1] if text else ""
            if last_char in [".", "!", "?", "…"]:
                custom_pad_end = 500  # 500 ms pause after sentences
            elif last_char in [",", ";", ":"]:
                custom_pad_end = 250  # 250 ms pause after clauses
            else:
                custom_pad_end = 250
                
            int16_samples = trim_silence(int16_samples, 24000, pad_end_ms=custom_pad_end)

            # Save wav file (StyleTTS2 native sample rate is 24000 Hz)
            with wave.open(output_file, "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(24000)
                packed_data = struct.pack(f"{len(int16_samples)}h", *int16_samples)
                wav_file.writeframes(packed_data)

            # Update cache file dynamically
            cache[chunk_hash] = text
            try:
                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump(cache, f, ensure_ascii=False, indent=2)
            except Exception:
                pass

        except Exception as e:
            print(f"[TTSHelper] Error running StyleTTS2 on chunk {chunk_hash}: {e}", file=sys.stderr)

def main():
    try:
        payload = json.load(sys.stdin)
    except Exception as e:
        print(f"[TTSHelper] Error: Failed to parse JSON from stdin: {e}", file=sys.stderr)
        sys.exit(1)

    engine = payload.get("tts_engine", "supertonic3")
    if engine == "supertonic3":
        run_supertonic3(payload)
    elif engine == "styletts2":
        run_styletts2(payload)
    else:
        print(f"[TTSHelper] Error: Unsupported tts_engine '{engine}'", file=sys.stderr)
        sys.exit(1)

    print("[TTSHelper] Done processing chunks.", flush=True)

if __name__ == "__main__":
    main()
