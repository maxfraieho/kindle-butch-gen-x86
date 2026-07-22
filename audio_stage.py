#!/usr/bin/env python3
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

import re
import json
import hashlib
import argparse
import subprocess
import wave
import struct
from common.text_protect import PlaceholderManager
from common.book_paths import resolve_book_paths
from common.utils import get_hash

def get_sample_rate(wav_path):
    """Reads sample rate from a WAV file."""
    with wave.open(wav_path, "rb") as w:
        return w.getframerate()

def generate_silence_wav(output_path, duration_ms, sample_rate):
    """Generates a silence WAV file of given duration and sample rate."""
    num_samples = int((duration_ms / 1000.0) * sample_rate)
    silence_samples = [0] * num_samples
    with wave.open(output_path, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        packed_data = struct.pack(f"{num_samples}h", *silence_samples)
        wav_file.writeframes(packed_data)

def apply_fade_out(input_wav, output_wav, fade_ms=15):
    """Applies a fade-out to the end of a WAV file via ffmpeg."""
    fade_sec = fade_ms / 1000.0
    import shutil
    if shutil.which("proot-distro"):
        cmd = [
            "proot-distro", "login", "ubuntu", "--",
            "ffmpeg", "-y", "-i", os.path.abspath(input_wav),
            "-af", f"areverse,afade=t=in:d={fade_sec},areverse",
            os.path.abspath(output_wav)
        ]
    else:
        cmd = [
            "ffmpeg", "-y", "-i", input_wav,
            "-af", f"areverse,afade=t=in:d={fade_sec},areverse",
            output_wav
        ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def log(message):
    print(f"[AudioStage] {message}", flush=True)


def locate_translated_file(book_dir, target_lang):
    # Check 1: books/<slug>/translated/merged_translated_<target_lang>.md
    path1 = os.path.join(book_dir, "translated", f"merged_translated_{target_lang}.md")
    if os.path.exists(path1):
        return path1

    # Check 2: look for any .md file ending with _translated_<target_lang>.md under books/<slug>/translated/
    translated_dir = os.path.join(book_dir, "translated")
    if os.path.exists(translated_dir):
        for f in os.listdir(translated_dir):
            if f.endswith(f"_translated_{target_lang}.md"):
                return os.path.join(translated_dir, f)

    # Check 3: look under books/<slug>/output/
    output_dir = os.path.join(book_dir, "output")
    if os.path.exists(output_dir):
        for f in os.listdir(output_dir):
            if f.endswith(f"_translated_{target_lang}.md"):
                return os.path.join(output_dir, f)

    # Check 4: books/<slug>/translated/merged_source_{target_lang}.md
    path_source = os.path.join(book_dir, "translated", f"merged_source_{target_lang}.md")
    if os.path.exists(path_source):
        return path_source

    # Check 5: look for any .md file ending with _source_{target_lang}.md under books/<slug>/translated/
    if os.path.exists(translated_dir):
        for f in os.listdir(translated_dir):
            if f.endswith(f"_source_{target_lang}.md"):
                return os.path.join(translated_dir, f)

    # Check 6: look under books/<slug>/output/ for ending with _source_{target_lang}.md
    if os.path.exists(output_dir):
        for f in os.listdir(output_dir):
            if f.endswith(f"_source_{target_lang}.md"):
                return os.path.join(output_dir, f)

    return None

def split_paragraph_to_chunks(text, max_chars=1000):
    # Strip any stray placeholders of the form __PREFIX_ID__ BEFORE formatting is stripped
    text = re.sub(r"__[A-Z_]+_\d+__", "", text)
    # Strip formatting from paragraph first
    clean_text = PlaceholderManager.strip_formatting(text).strip()
    if not clean_text:
        return []

    # If within limit, return as single chunk
    if len(clean_text) <= max_chars:
        return [clean_text]

    # Split by sentence endings (. ! ?) followed by whitespace
    sentences = re.split(r'(?<=[.!?])\s+', clean_text)
    chunks = []
    curr_group = []
    curr_len = 0

    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue

        # If a single sentence exceeds 1000 characters, chunk it by words
        if len(sentence) > max_chars:
            if curr_group:
                chunks.append(" ".join(curr_group))
                curr_group = []
                curr_len = 0
            
            words = sentence.split(" ")
            word_group = []
            word_len = 0
            for w in words:
                if word_len + len(w) + 1 > max_chars:
                    if word_group:
                        chunks.append(" ".join(word_group))
                    word_group = [w]
                    word_len = len(w)
                else:
                    word_group.append(w)
                    word_len += len(w) + 1
            if word_group:
                chunks.append(" ".join(word_group))
        else:
            # Check if adding the sentence would exceed the limit
            if curr_len + len(sentence) + (1 if curr_group else 0) > max_chars:
                if curr_group:
                    chunks.append(" ".join(curr_group))
                curr_group = [sentence]
                curr_len = len(sentence)
            else:
                curr_group.append(sentence)
                curr_len += len(sentence) + (1 if len(curr_group) > 1 else 0)

    if curr_group:
        chunks.append(" ".join(curr_group))

    return chunks

def main():
    parser = argparse.ArgumentParser(description="Audio generation stage for translated books")
    parser.add_argument("--book", "-b", required=False, help="Book slug")
    parser.add_argument("--config", "-c", help="Optional path to config.json")
    args = parser.parse_args()

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
        log("Error: Book slug is required.")
        sys.exit(1)

    paths = resolve_book_paths(repo_dir, slug, config_path=args.config)
    target_lang = paths.get("target_lang", "uk")
    voice = paths.get("tts_voice", "lada")
    voice_quality = paths.get("tts_voice_quality", "x_low")
    speaker_id = paths.get("tts_speaker_id", 2)
    speed = paths.get("tts_speed", 1.0)
    noise_scale = paths.get("tts_noise_scale", 0.667)
    noise_w = paths.get("tts_noise_w", 0.8)

    # Pick voice based on target_lang and voice/quality config
    lang_info = {
        "uk": {
            "code": "uk_UA",
            "hf_dir": "uk/uk_UA",
            "default_voice": "ukrainian_tts",
            "default_quality": "medium",
            "valid_voices": ["ukrainian_tts", "lada"]
        },
        "ru": {
            "code": "ru_RU",
            "hf_dir": "ru/ru_RU",
            "default_voice": "irina",
            "default_quality": "medium",
            "valid_voices": ["irina", "denis", "dmitri", "ruslan"]
        }
    }
    
    info = lang_info.get(target_lang, lang_info["uk"])
    
    # Validate voice for the given language
    if voice not in info["valid_voices"]:
        voice = info["default_voice"]
        
    # Standardize quality parameter
    if voice_quality not in ["low", "medium", "high", "x_low"]:
        voice_quality = info["default_quality"]
        
    lang_code = info["code"]
    hf_dir = info["hf_dir"]
    
    tts_engine = paths.get("tts_engine", "supertonic3")
    if tts_engine == "styletts2":
        voice_slug = "styletts2"
        model_path = os.path.join(repo_dir, "models", "styletts2", "model.onnx")
    else:
        tts_engine = "supertonic3"
        voice_slug = "supertonic-3-tts-int8"
        model_path = ""

    # Set up directories
    book_dir = paths["book_dir"]
    chunks_dir = os.path.join(paths["audio_dir"], f"chunks_{voice_slug}")
    cache_path = os.path.join(paths["cache_dir"], f"tts_cache_{voice_slug}.json")
    output_dir = paths["output_dir"]

    os.makedirs(chunks_dir, exist_ok=True)
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    # Locate the translated markdown file
    translated_file = locate_translated_file(book_dir, target_lang)
    if not translated_file:
        log(f"Error: Could not locate translated markdown file for target language '{target_lang}' in '{book_dir}'")
        sys.exit(1)

    log(f"Using translated file: {translated_file}")

    # Read markdown and split into paragraphs
    with open(translated_file, "r", encoding="utf-8") as f:
        content = f.read()

    # Split by double newlines
    paragraphs = re.split(r'\n\s*\n', content)
    log(f"Total paragraphs read: {len(paragraphs)}")

    # Filter out TOC, publisher technical pages, and other metadata
    filtered_paragraphs = []
    skip_section = False
    
    skip_heading_keywords = [
        "зміст", 
        "передмова від видавництва", 
        "про авторів", 
        "предметний покажчик", 
        "відгуки", 
        "список помилок", 
        "порушення авторських прав"
    ]
    
    for p in paragraphs:
        p_clean = p.strip()
        if not p_clean:
            continue
            
        # Check if the paragraph is a heading
        is_heading = p_clean.startswith("#")
        if is_heading:
            heading_text = re.sub(r'[#*_\s]+', ' ', p_clean).strip().lower()
            if any(keyword in heading_text for keyword in skip_heading_keywords):
                skip_section = True
                log(f"[AudioStage] Skipping section under heading: '{p_clean}'")
            else:
                skip_section = False
                
        if skip_section:
            continue
            
        # Check if it is a standalone copyright/publication details block
        p_lower = p_clean.lower()
        is_publication_page = (
            ("удк" in p_lower and "ббк" in p_lower) or 
            ("isbn" in p_lower and "видавництво" in p_lower) or 
            ("isbn" in p_lower and "издательство" in p_lower) or
            ("усі права захищені" in p_lower) or
            ("все права защищены" in p_lower)
        )
        if is_publication_page:
            log(f"[AudioStage] Skipping publication/copyright page paragraph: '{p_clean[:60]}...'")
            continue
            
        filtered_paragraphs.append(p)

    log(f"Filtered paragraphs count (excluding TOC and technical details): {len(filtered_paragraphs)}")

    # Clean formatting and split paragraphs if they exceed 1000 characters
    # For StyleTTS2 we enforce a much lower max_chars limit to avoid ONNX broadcast errors
    max_chunk_chars = 150 if tts_engine == "styletts2" else 1000
    chunk_texts = []
    header_hashes = set()
    for p in filtered_paragraphs:
        is_heading = p.strip().startswith("#")
        chunks = split_paragraph_to_chunks(p, max_chars=max_chunk_chars)
        for chunk in chunks:
            chunk = chunk.strip()
            if chunk:
                chunk_texts.append(chunk)
                if is_heading:
                    header_hashes.add(get_hash(chunk))

    log(f"Total TTS text chunks to synthesize/verify: {len(chunk_texts)}")

    # Load TTS cache
    cache = {}
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cache = json.load(f)
            log(f"Loaded cache with {len(cache)} entries.")
        except Exception as e:
            log(f"Warning: Failed to load TTS cache: {e}. Starting fresh.")

    # Check for missing chunks
    missing_chunks = []
    chunk_hashes = []
    for text in chunk_texts:
        chunk_hash = get_hash(text)
        chunk_hashes.append(chunk_hash)
        
        wav_file = os.path.join(chunks_dir, f"{chunk_hash}.wav")
        # A chunk is valid if it is in cache AND the actual WAV file exists on disk
        if chunk_hash not in cache or not os.path.exists(wav_file):
            missing_chunks.append({
                "hash": chunk_hash,
                "text": text
            })

    # Keep only unique missing chunks for synthesis to avoid duplicate processing
    unique_missing_chunks = []
    seen_hashes = set()
    for mc in missing_chunks:
        if mc["hash"] not in seen_hashes:
            seen_hashes.add(mc["hash"])
            unique_missing_chunks.append(mc)

    log(f"Missing WAV chunks: {len(unique_missing_chunks)} (unique) / {len(missing_chunks)} (total)")

    # Synthesize missing chunks if any
    if unique_missing_chunks:
        # Acquire termux-wake-lock
        try:
            log("Acquiring termux-wake-lock...")
            subprocess.run(["termux-wake-lock"], check=False)
        except Exception as e:
            log(f"Warning: termux-wake-lock failed: {e}")

        try:
            # Preprocess chunks: run stressify_batch.py inside PRoot container if target language is Ukrainian
            if target_lang == "uk":
                try:
                    stress_cache_path = os.path.join(book_dir, "translated", f"stress_cache_{target_lang}.json")
                    stress_cache = {}
                    if os.path.exists(stress_cache_path):
                        try:
                            with open(stress_cache_path, "r", encoding="utf-8") as f:
                                stress_cache = json.load(f)
                            log(f"Loaded stress cache with {len(stress_cache)} entries.")
                        except Exception as e:
                            log(f"Warning: Failed to load stress cache: {e}")

                    # Determine which chunks need to be stressified (those not in stress_cache)
                    to_stressify = []
                    for mc in unique_missing_chunks:
                        h = mc["hash"]
                        if h in stress_cache:
                            mc["text"] = stress_cache[h]
                        else:
                            to_stressify.append(mc)

                    if to_stressify:
                        log(f"Запуск пакетного розставлення наголосів (stressifier) для {len(to_stressify)} нових фрагментів у середовищі PRoot...")
                        temp_input = os.path.join(repo_dir, "books", "temp_unstressed.json")
                        temp_output = os.path.join(repo_dir, "books", "temp_stressed.json")
                        
                        with open(temp_input, "w", encoding="utf-8") as f:
                            json.dump({
                                "chunks": to_stressify,
                                "lang": target_lang
                            }, f, ensure_ascii=False, indent=2)
                        cmd_stress = [
                            "proot-distro", "login", "ubuntu", "--",
                            "python3", os.path.join(repo_dir, "bin", "stressify_batch.py")
                        ]
                        subprocess.run(cmd_stress, stdin=subprocess.DEVNULL, check=True)
                        
                        if os.path.exists(temp_output):
                            with open(temp_output, "r", encoding="utf-8") as f:
                                stressed_data = json.load(f)
                            
                            # Update stress_cache and unique_missing_chunks
                            for c in stressed_data.get("chunks", []):
                                h = c["hash"]
                                t = c["text"]
                                stress_cache[h] = t
                                # Update mc in unique_missing_chunks
                                for mc in unique_missing_chunks:
                                    if mc["hash"] == h:
                                        mc["text"] = t

                            # Save updated stress_cache
                            try:
                                with open(stress_cache_path, "w", encoding="utf-8") as f:
                                    json.dump(stress_cache, f, ensure_ascii=False, indent=2)
                                log("Збережено оновлений кеш наголосів.")
                            except Exception as e:
                                log(f"Попередження: Не вдалося зберегти кеш наголосів: {e}")
                        else:
                            log("Попередження: temp_stressed.json не знайдено. Продовження з вихідним текстом.")
                            
                        if os.path.exists(temp_input):
                            os.remove(temp_input)
                        if os.path.exists(temp_output):
                            os.remove(temp_output)
                    else:
                        log("Усі фрагменти вже є в кеші наголосів. Пропуск розстановки наголосів.")
                except Exception as e:
                    log(f"Попередження: Попередня обробка (stressifier/NFD) завершилася з помилкою: {e}. Продовження з вихідним текстом.")

            # Prepare payload for tts_helper.py
            # TASK-23: kbg_web/app.py's edit_regenerate_audio writes queued
            # live-edit chunks here (same voice_slug) while this run is
            # active - tts_helper.py's own loop picks them up per-chunk.
            audio_priority_path = os.path.join(paths["audio_dir"], f"audio_priority_{voice_slug}.json")

            payload = {
                "tts_engine": tts_engine,
                "model_path": model_path,
                "output_dir": os.path.abspath(chunks_dir),
                "cache_path": os.path.abspath(cache_path),
                "chunks": unique_missing_chunks,
                "speaker_id": speaker_id,
                "speed": speed,
                "lang": target_lang,
                "audio_priority_path": os.path.abspath(audio_priority_path),
                "slug": slug
            }
            if tts_engine == "styletts2":
                payload["voice_quality"] = voice_quality
                payload["noise_scale"] = noise_scale
                payload["noise_w"] = noise_w
            payload_json = json.dumps(payload, ensure_ascii=False)

            helper_path = os.path.join(repo_dir, "bin", "tts_helper.py")
            
            # Call tts_helper.py natively in Termux
            cmd = [
                sys.executable, helper_path
            ]
            
            log(f"Invoking tts_helper.py (engine: {tts_engine})...")
            subprocess.run(
                cmd,
                input=payload_json,
                text=True,
                check=True
            )

        except subprocess.CalledProcessError as e:
            log(f"Error: tts_helper.py failed with exit code {e.returncode}")
            sys.exit(1)
        except Exception as e:
            log(f"Error calling tts_helper.py: {e}")
            sys.exit(1)
        finally:
            # Release termux-wake-unlock
            try:
                log("Releasing termux-wake-unlock...")
                subprocess.run(["termux-wake-unlock"], check=False)
            except Exception as e:
                log(f"Warning: termux-wake-unlock failed: {e}")

        # NOTE (TASK-23): tts_helper.py itself writes cache_path
        # incrementally after each chunk (both engines) - it's the
        # authoritative writer, not just this process's local `cache` dict
        # loaded before the subprocess ran. We deliberately do NOT
        # re-save `cache` here anymore: this process's copy predates any
        # chunks tts_helper.py spliced in from the live-edit priority
        # queue while running, so overwriting cache_path with it would
        # silently drop those entries right after tts_helper.py wrote them.

    # Verify all chunk files exist before proceeding
    missing_files = []
    for h in chunk_hashes:
        wav_file = os.path.join(chunks_dir, f"{h}.wav")
        if not os.path.exists(wav_file):
            missing_files.append(wav_file)

    if missing_files:
        log(f"Error: {len(missing_files)} chunk WAV files are missing even after synthesis. Aborting concatenation.")
        for mf in missing_files[:5]:
            log(f"  Missing: {mf}")
        sys.exit(1)

    log("All chunk files verified successfully.")

    # Run ASR verification loop if enabled
    if paths.get("enable_asr_verify", False):
        model_dir = os.path.join(repo_dir, "models", "sherpa-onnx-whisper-small-int8")
        alt_model_dir = os.path.expanduser("~/models/sherpa-onnx-whisper-small-int8")
        target_model_dir = model_dir if os.path.exists(model_dir) else alt_model_dir

        encoder_ok = any(os.path.isfile(os.path.join(target_model_dir, f)) and os.path.getsize(os.path.join(target_model_dir, f)) > 10000000 for f in ["small-encoder.int8.onnx", "encoder.int8.onnx", "encoder.onnx"] if os.path.exists(os.path.join(target_model_dir, f)))
        decoder_ok = any(os.path.isfile(os.path.join(target_model_dir, f)) and os.path.getsize(os.path.join(target_model_dir, f)) > 10000000 for f in ["small-decoder.int8.onnx", "decoder.int8.onnx", "decoder.onnx"] if os.path.exists(os.path.join(target_model_dir, f)))
        tokens_ok = any(os.path.isfile(os.path.join(target_model_dir, f)) and os.path.getsize(os.path.join(target_model_dir, f)) > 10000 for f in ["small-tokens.txt", "tokens.txt"] if os.path.exists(os.path.join(target_model_dir, f)))

        if not (os.path.exists(target_model_dir) and encoder_ok and decoder_ok and tokens_ok):
            log("Попередження: Прапорець enable_asr_verify увімкнено, але моделі Whisper ASR відсутні або не повністю завантажені. Пропуск верифікації наголосів.")
        else:
            log("Running ASR verification on newly synthesized chunks...")
            try:
                from common.asr_verify import verify_chunk, append_to_stress_queue
                
                queue_path = os.path.join(paths["book_dir"], "asr_stress_queue.json")
                
                if unique_missing_chunks:
                    log(f"Analyzing {len(unique_missing_chunks)} new chunks using Whisper...")
                    for mc in unique_missing_chunks:
                        chunk_hash = mc["hash"]
                        chunk_text = mc["text"]
                        audio_path = os.path.join(chunks_dir, f"{chunk_hash}.wav")
                        
                        if os.path.exists(audio_path):
                            flag = verify_chunk(
                                chunk_id=chunk_hash,
                                audio_path=audio_path,
                                original_text=chunk_text,
                                model_dir=target_model_dir,
                                cer_threshold=0.15,
                                language=target_lang,
                            )
                            if flag["mismatch"]:
                                append_to_stress_queue(flag, queue_path)
                                log(f"  ASR Mismatch on {chunk_hash} (CER: {flag['char_error_rate']:.4f}). "
                                    f"Ref: '{chunk_text}' | Hyp: '{flag['transcribed_text']}'")
                else:
                    log("No new chunks were synthesized in this run. Skipping ASR analysis.")
            except Exception as e:
                log(f"Warning: ASR verification loop failed: {e}")

    # Create ffmpeg list file with silence and fade-out
    log("Preparing chunks with fade-out and silence padding...")

    # Determine Sample Rate from the first chunk for generating proper silence
    sample_rate = 22050  # Default fallback
    if chunk_hashes:
        first_chunk = os.path.join(chunks_dir, f"{chunk_hashes[0]}.wav")
        if os.path.exists(first_chunk):
            try:
                sample_rate = get_sample_rate(first_chunk)
            except Exception as e:
                log(f"Warning: Failed to read sample rate from first chunk: {e}")

    temp_dir = os.path.join(paths["audio_dir"], "temp_processing")
    os.makedirs(temp_dir, exist_ok=True)

    # Generate silence WAV files
    silence_500_path = os.path.join(temp_dir, "silence_500.wav")
    silence_3000_path = os.path.join(temp_dir, "silence_3000.wav")

    try:
        generate_silence_wav(silence_500_path, 500, sample_rate)
        generate_silence_wav(silence_3000_path, 3000, sample_rate)
    except Exception as e:
        log(f"Error generating silence files: {e}")
        sys.exit(1)

    ffmpeg_list_path = os.path.join(paths["audio_dir"], "ffmpeg_list.txt")
    try:
        with open(ffmpeg_list_path, "w", encoding="utf-8") as lf:
            for idx, h in enumerate(chunk_hashes):
                chunk_file = os.path.abspath(os.path.join(chunks_dir, f"{h}.wav"))
                
                # Apply 15ms fade-out to prevent clicks
                faded_chunk = os.path.join(temp_dir, f"{h}_faded.wav")
                if not os.path.exists(faded_chunk):
                    apply_fade_out(chunk_file, faded_chunk, fade_ms=15)
                
                escaped_chunk = faded_chunk.replace("'", "'\\''")
                lf.write(f"file '{escaped_chunk}'\n")

                # Insert appropriate silence between chunks
                if idx < len(chunk_hashes) - 1:
                    next_hash = chunk_hashes[idx + 1]
                    is_next_heading = next_hash in header_hashes
                    silence_file = silence_3000_path if is_next_heading else silence_500_path
                    
                    escaped_silence = os.path.abspath(silence_file).replace("'", "'\\''")
                    lf.write(f"file '{escaped_silence}'\n")
    except Exception as e:
        log(f"Error writing ffmpeg list file: {e}")
        sys.exit(1)

    # Concatenate and encode to MP3
    output_mp3 = os.path.join(output_dir, f"{slug}_translated_{target_lang}.mp3")
    log(f"Concatenating chunks and encoding to: {output_mp3}")

    cmd_ffmpeg = [
        "proot-distro", "login", "ubuntu", "--",
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", os.path.abspath(ffmpeg_list_path),
        "-af", "afftdn,highpass=f=80,lowpass=f=8000,speechnorm",
        "-c:a", "libmp3lame", "-q:a", "4",
        os.path.abspath(output_mp3)
    ]

    try:
        subprocess.run(
            cmd_ffmpeg,
            check=True
        )
    except subprocess.CalledProcessError as e:
        log(f"Error: ffmpeg compilation/concatenation failed with exit code {e.returncode}")
        sys.exit(1)
    finally:
        # Clean up temporary ffmpeg list file
        if os.path.exists(ffmpeg_list_path):
            try:
                os.remove(ffmpeg_list_path)
            except Exception as e:
                log(f"Warning: Failed to remove temporary ffmpeg list file: {e}")

    log(f"Audiobook generation completed successfully! Saved to: {output_mp3}")

if __name__ == "__main__":
    main()
