#!/usr/bin/env python3
"""
common/asr_verify.py  —  TASK-86: ASR-петля верифікації наголосів

Standalone-модуль.  НЕ інтегрований у audio_stage.py (чекає підтвердження Q
щодо реального встановлення whisper.cpp / sherpa-onnx-whisper-model).

Призначення:
  Після TTS-синтезу чанка — транскрибувати аудіо через `whisper-cli` (або
  сумісний бінарник: sherpa-onnx-offline-asr, будь-який CLI з --file → stdout),
  порівняти з оригінальним текстом через Левенштейна, і повернути mismatch_flag
  у форматі quality_flags.json (той самий патерн, що translate_manga.py →
  post_render_check → quality_flags.json; TASK-19/20).

Аlternative backend:
  sherpa-onnx (Python package) вже встановлений на пристрої Q (версія 1.13.4,
  підтримує OfflineRecognizer.from_whisper()).  Коли буде завантажена ONNX-модель
  Whisper Small (доступна у репозиторії k2-fsa/sherpa-onnx), цей модуль може
  бути розширений без нового CLI-бінарника — достатньо замінити `transcribe()`
  або додати `transcribe_via_sherpa_onnx()`.  Потребує окремого підтвердження Q.

Безпека:
  Жоден важкий процес (компіляція, завантаження моделі, inference) не
  запускається при import цього модуля.  subprocess-виклик відбувається ТІЛЬКИ
  в `transcribe()`, і тільки якщо явно передати шлях до існуючого бінарника.

Формат mismatch_flag (відповідає quality_flags.json):
  {
    "chunk_id":       "slug_chunkXX",   # str  —  ідентифікатор чанка
    "audio_path":     "...",            # str  —  шлях до WAV/MP3
    "original_text":  "...",            # str  —  оригінал (після TTS-підготовки)
    "transcribed_text": "...",          # str  —  що почув ASR
    "levenshtein_distance": 12,         # int
    "char_error_rate":  0.15,           # float  —  CER = edit_dist / max(len_ref, 1)
    "mismatch":       True,             # bool  —  True якщо CER > threshold
    "reason":         "asr_mismatch",   # str  —  константа для фільтрації
    "cer_threshold":  0.15,             # float  —  поріг на момент перевірки
    "asr_backend":    "whisper-cli",    # str  —  "whisper-cli" | "sherpa-onnx" | "mock"
    "asr_model":      "...",            # str  —  шлях до моделі або назва
  }
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from typing import Optional

# ---------------------------------------------------------------------------
# Levensht ein — чиста Python-реалізація (без залежностей)
# Перевірено: python-Levenshtein / rapidfuzz НЕ використовуються у проєкті,
# тому додаємо власну O(n*m) реалізацію (достатньо для рядків < 1000 символів).
# ---------------------------------------------------------------------------

def levenshtein_distance(s1: str, s2: str) -> int:
    """Classic DP Levenshtein.  O(len(s1) * len(s2)) time and space.

    For short TTS chunk texts (< 500 chars) this is fast enough.
    If s1 or s2 is empty — returns len of the other (insert/delete all).
    """
    if s1 == s2:
        return 0
    len1, len2 = len(s1), len(s2)
    if len1 == 0:
        return len2
    if len2 == 0:
        return len1

    # Two-row rolling array to save memory
    prev = list(range(len2 + 1))
    curr = [0] * (len2 + 1)
    for i, c1 in enumerate(s1, 1):
        curr[0] = i
        for j, c2 in enumerate(s2, 1):
            if c1 == c2:
                curr[j] = prev[j - 1]
            else:
                curr[j] = 1 + min(prev[j], curr[j - 1], prev[j - 1])
        prev, curr = curr, prev
    return prev[len2]


def char_error_rate(ref: str, hyp: str) -> float:
    """CER = levenshtein_distance / max(len(ref), 1).

    Returns float in [0, ∞).  Values > 1.0 mean the ASR produced more
    characters than the reference (happens with hallucinations).
    """
    return levenshtein_distance(ref, hyp) / max(len(ref), 1)


# ---------------------------------------------------------------------------
# Нормалізація тексту перед порівнянням
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    """Strip extra whitespace and lower-case for comparison only.

    Does NOT modify Ukrainian stress marks (´) so the comparison works
    both with and without accent marks in the transcribed output.
    """
    import re
    text = text.strip()
    # Collapse multiple spaces/newlines to single space
    text = re.sub(r"\s+", " ", text)
    return text.lower()


# ---------------------------------------------------------------------------
# Транскрипція через sherpa-onnx Python API
# ---------------------------------------------------------------------------

_recognizer = None

def transcribe(
    audio_path: str,
    model_dir: str,
    language: str = "uk",
    num_threads: int = 4,
) -> str:
    """Call sherpa-onnx (Whisper model) Python API and return transcribed text.

    Args:
        audio_path:  Existing WAV file to transcribe.
        model_dir:   Path to the directory containing Whisper ONNX model files:
                     encoder.onnx, decoder.onnx, tokens.txt.
        language:    Whisper language code (default "uk" for Ukrainian).
        num_threads: Number of threads to use for computation.

    Returns:
        Transcribed text as a plain string.

    Raises:
        FileNotFoundError: if model_dir, audio_path, or model files do not exist.
    """
    global _recognizer

    if not os.path.isdir(model_dir):
        raise FileNotFoundError(f"Whisper model directory not found: {model_dir}")
    if not os.path.isfile(audio_path):
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    # Lazy imports to support running tests on systems without these packages installed
    import sherpa_onnx
    import numpy as np
    import wave

    if _recognizer is None:
        # Resolve required files inside model_dir, prioritizing int8 quantized versions
        encoder = None
        for name in ["encoder.int8.onnx", "small-encoder.int8.onnx", "encoder.onnx", "small-encoder.onnx"]:
            path = os.path.join(model_dir, name)
            if os.path.exists(path):
                encoder = path
                break
        if encoder is None:
            # Fallback search
            for f in os.listdir(model_dir):
                if f.endswith("encoder.int8.onnx") or f.endswith("encoder.onnx"):
                    encoder = os.path.join(model_dir, f)
                    break

        decoder = None
        for name in ["decoder.int8.onnx", "small-decoder.int8.onnx", "decoder.onnx", "small-decoder.onnx"]:
            path = os.path.join(model_dir, name)
            if os.path.exists(path):
                decoder = path
                break
        if decoder is None:
            # Fallback search
            for f in os.listdir(model_dir):
                if f.endswith("decoder.int8.onnx") or f.endswith("decoder.onnx"):
                    decoder = os.path.join(model_dir, f)
                    break

        tokens = None
        for name in ["tokens.txt", "small-tokens.txt"]:
            path = os.path.join(model_dir, name)
            if os.path.exists(path):
                tokens = path
                break
        if tokens is None:
            # Fallback search
            for f in os.listdir(model_dir):
                if f.endswith("tokens.txt"):
                    tokens = os.path.join(model_dir, f)
                    break

        if not encoder or not decoder or not tokens or not os.path.isfile(encoder) or not os.path.isfile(decoder) or not os.path.isfile(tokens):
            raise FileNotFoundError(
                f"Missing required Whisper model files in {model_dir}.\n"
                f"Expected encoder.onnx, decoder.onnx, tokens.txt."
            )

        _recognizer = sherpa_onnx.OfflineRecognizer.from_whisper(
            encoder=encoder,
            decoder=decoder,
            tokens=tokens,
            num_threads=num_threads,
            language=language,
            task="transcribe",
        )

    # Read mono/16-bit WAV file and normalize to float32
    with wave.open(audio_path, "rb") as f:
        n_channels = f.getnchannels()
        sampwidth = f.getsampwidth()
        sample_rate = f.getframerate()
        num_frames = f.getnframes()
        
        samples_bytes = f.readframes(num_frames)
        
        if sampwidth == 2:
            samples_int16 = np.frombuffer(samples_bytes, dtype=np.int16)
            samples_float32 = samples_int16.astype(np.float32) / 32768.0
        elif sampwidth == 1:
            samples_int8 = np.frombuffer(samples_bytes, dtype=np.uint8)
            samples_float32 = (samples_int8.astype(np.float32) - 128.0) / 128.0
        else:
            raise ValueError(f"Unsupported sample width: {sampwidth}")
            
        if n_channels > 1:
            samples_float32 = samples_float32[::n_channels]

    # Run recognizer on the waveform
    stream = _recognizer.create_stream()
    stream.accept_waveform(sample_rate, samples_float32)
    _recognizer.decode_stream(stream)
    
    return stream.result.text.strip()


# ---------------------------------------------------------------------------
# Побудова mismatch_flag (якісний формат quality_flags.json)
# ---------------------------------------------------------------------------

def build_mismatch_flag(
    chunk_id: str,
    audio_path: str,
    original_text: str,
    transcribed_text: str,
    cer_threshold: float = 0.15,
    asr_backend: str = "sherpa-onnx",
    asr_model: str = "",
) -> dict:
    """Build a mismatch_flag dict for quality_flags.json / stress review queue.

    The format mirrors translate_manga.py's post_render_check() flags:
    {
      "chunk_id": ..., "reason": "asr_mismatch",
      "mismatch": True/False, "char_error_rate": float, ...
    }

    A flag is always returned (mismatch=False if CER <= threshold).
    The caller decides whether to append to the queue (typically: only if
    mismatch=True, but keeping all flags is also valid for auditing).

    Args:
        chunk_id:        Unique identifier for this TTS chunk.
        audio_path:      Path to the synthesized WAV (for reference/playback).
        original_text:   The text that was fed to TTS.
        transcribed_text: The text returned by the ASR backend.
        cer_threshold:   CER above which mismatch=True.  Default 0.15 (15%).
        asr_backend:     Informational: "sherpa-onnx" | "mock".
        asr_model:       Informational: model path or name used.

    Returns:
        dict with keys matching the format docstring at module top.
    """
    ref = _normalize(original_text)
    hyp = _normalize(transcribed_text)
    dist = levenshtein_distance(ref, hyp)
    cer = char_error_rate(ref, hyp)
    is_mismatch = cer > cer_threshold

    return {
        "chunk_id": chunk_id,
        "audio_path": audio_path,
        "original_text": original_text,
        "transcribed_text": transcribed_text,
        "levenshtein_distance": dist,
        "char_error_rate": round(cer, 4),
        "mismatch": is_mismatch,
        "reason": "asr_mismatch" if is_mismatch else "asr_ok",
        "cer_threshold": cer_threshold,
        "asr_backend": asr_backend,
        "asr_model": asr_model,
    }


# ---------------------------------------------------------------------------
# Запис черги у JSON-файл (аналог quality_flags.json для audio-пайплайну)
# ---------------------------------------------------------------------------

def append_to_stress_queue(flag: dict, queue_path: str) -> None:
    """Append a mismatch_flag to the stress review queue JSON file.

    The queue file is a JSON array (same convention as quality_flags.json).
    If the file does not exist, it is created.  Thread-unsafe (single-process
    assumption, same as quality_flags.json usage elsewhere in the project).

    Args:
        flag:       Dict returned by build_mismatch_flag().
        queue_path: Path to the JSON queue file, e.g.
                    "books/<slug>/asr_stress_queue.json"
    """
    if os.path.exists(queue_path):
        with open(queue_path, "r", encoding="utf-8") as f:
            try:
                queue = json.load(f)
                if not isinstance(queue, list):
                    queue = []
            except (json.JSONDecodeError, ValueError):
                queue = []
    else:
        queue = []

    queue.append(flag)

    # Atomic write (tmp + os.replace) - same convention as
    # kbg_web/app.py's _atomic_write_json and cast_ner_prepass.py's
    # save_progress.
    tmp_path = queue_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(queue, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, queue_path)


# ---------------------------------------------------------------------------
# High-level verify function: транскрибувати + побудувати flag в один виклик
# ---------------------------------------------------------------------------

def verify_chunk(
    chunk_id: str,
    audio_path: str,
    original_text: str,
    model_dir: str,
    cer_threshold: float = 0.15,
    language: str = "uk",
    num_threads: int = 4,
) -> dict:
    """Transcribe audio and build a mismatch_flag in one call.

    This is the main entry point for the ASR loop in audio_stage.py.
    If transcription fails for any reason, returns a flag with
    transcribed_text="<error: ...>" and mismatch=True so the chunk is always
    queued for manual review on ASR failure (fail-safe, not fail-silent).

    Args:  (see transcribe() and build_mismatch_flag() for details)

    Returns:
        mismatch_flag dict.
    """
    try:
        transcribed = transcribe(
            audio_path=audio_path,
            model_dir=model_dir,
            language=language,
            num_threads=num_threads,
        )
        asr_backend = "sherpa-onnx"
    except Exception as exc:
        transcribed = f"<error: {exc}>"
        asr_backend = "sherpa-onnx-error"

    return build_mismatch_flag(
        chunk_id=chunk_id,
        audio_path=audio_path,
        original_text=original_text,
        transcribed_text=transcribed,
        cer_threshold=cer_threshold,
        asr_backend=asr_backend,
        asr_model=model_dir,
    )
