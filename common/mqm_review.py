#!/usr/bin/env python3
"""
common/mqm_review.py  —  TASK-88: MQM семантична валідація перекладу через reflection

Standalone-модуль.  НЕ інтегрований у translate_segment_with_retry / основний pipeline
(чекає підтвердження Q — той самий патерн, що TASK-86's asr_verify.py).

Призначення:
  Після генерації пакету перекладених абзаців — надіслати ОКРЕМИЙ stateless виклик до
  /completion-ендпоінту того самого llama-server (Hy-MT2-7B), але з новим системним
  промптом, що перетворює модель на MQM-аналізатора (Multidimensional Quality Metrics).

  Оскільки translate_text_hy_mt2() вже робить stateless виклики (кожен виклик надсилає
  ПОВНИЙ промпт заново, без переносу chat-history між викликами), MQM-виклик — це просто
  ЩЕ ОДИН незалежний /completion виклик. «Очищення контексту» НЕ потрібне технічно
  (його вже й так немає що чистити).  Але новий system-level промпт у іншій ролі усуває
  «consistent hallucination blind spot»: модель оцінює текст у ролі критика, а не автора.

Формат виводу MQM-флага (translation_quality_flags.json):
  {
    "segment_id":   "slug_para_042",   # str  — ідентифікатор сегмента
    "original":     "...",             # str  — оригінальний текст
    "translated":   "...",             # str  — перекладений текст
    "source_lang":  "ru",              # str
    "target_lang":  "uk",              # str
    "score":        7,                 # int | null  — 1-10 або null якщо parse failed
    "accept":       false,             # bool  — false = потребує review
    "issues":       ["...", "..."],    # list[str]  — описи проблем
    "reason":       "mqm_low_score",   # str  — константа для фільтрації
    "mqm_model":    "hy-mt2",          # str  — ідентифікатор моделі
  }

Поріг запису у файл:
  score < 7 АБО accept == False.  Поріг 7 обрано як «прохідна оцінка MQM-аналізатора»
  — менше 7 вказує на суттєву семантичну проблему (пропуск інформації, викривлення смислу,
  неперекладена сутність).  Флаги з score >= 7 AND accept=True — це «ок», їх не пишемо,
  щоб не роздувати файл.  Флаги з score=null (parse failure) пишемо завжди — це означає
  що MQM-виклик дав сміттєвий результат і сегмент варто перевірити вручну.
"""

from __future__ import annotations

import json
import os
import re
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# Константи
# ---------------------------------------------------------------------------

LANG_MAP: dict[str, str] = {
    "uk": "Ukrainian",
    "ru": "Russian",
    "en": "English",
    "hy": "Armenian",
}

# Поріг score нижче якого флаг записується у файл якості.
# Обґрунтування: 7/10 = MQM «minor issues» threshold — відповідає практиці
# проєктів де < 7 означає «major» або «critical» проблему якості перекладу.
MQM_SCORE_WRITE_THRESHOLD: int = 7

# n_predict для MQM відповіді — має бути достатньо для JSON ~200 токенів,
# але не надто велике щоб не затримувати pipeline.
MQM_N_PREDICT: int = 512

# Fail-safe результат при помилці парсингу.
MQM_PARSE_FAILURE: dict = {
    "score": None,
    "accept": True,
    "issues": ["MQM review failed to parse"],
}


# ---------------------------------------------------------------------------
# Побудова MQM-промпту
# ---------------------------------------------------------------------------

def _build_mqm_prompt(
    original: str,
    translated: str,
    source_lang_full: str,
    target_lang_full: str,
    is_7b_format: bool,
) -> tuple[str, list[str]]:
    """Побудувати raw_prompt і список stop_tokens для MQM-виклику /completion.

    Формат промпту навмисно відокремлений від translate_text_hy_mt2():
    тут системна роль — CRITIC, а не перекладач.  Повна система-промпт
    вбудована у raw_prompt (stateless /completion не має окремого system-поля).

    Returns:
        (raw_prompt, stop_tokens) — готово для POST до /completion.
    """
    # Інструкція однакова для 7B і не-7B — змінюється лише обрамлення токенів
    instruction = (
        f"You are a professional translation quality reviewer using MQM (Multidimensional "
        f"Quality Metrics) methodology. Your task is to evaluate the quality of a translation "
        f"from {source_lang_full} to {target_lang_full}.\n\n"
        f"Evaluate the translation for:\n"
        f"1. Information omissions (missing content from original)\n"
        f"2. Semantic distortions (meaning changes or hallucinations)\n"
        f"3. Untranslated entities (names, terms left in source language)\n\n"
        f"ORIGINAL ({source_lang_full}):\n{original}\n\n"
        f"TRANSLATION ({target_lang_full}):\n{translated}\n\n"
        f"Respond with ONLY a valid JSON object in this exact format (no markdown, no explanation):\n"
        f'{{ "score": <integer 1-10>, "accept": <true or false>, "issues": ["<issue description>", ...] }}\n\n'
        f"Where score 1-10 means: 1-4=poor (critical errors), 5-6=fair (major issues), "
        f"7-8=good (minor issues), 9-10=excellent. "
        f"Set accept=false if score < 7 or any critical error found. "
        f"If no issues found, use empty list for issues."
    )

    if is_7b_format:
        raw_prompt = f"<|startoftext|>{instruction}<|extra_0|>"
        stop_tokens = ["<|eos|>", "<|startoftext|>", "<|extra_0|>"]
    else:
        raw_prompt = (
            f"<|hy_begin\u2581of\u2581sentence|>"
            f"<|hy_User|>{instruction}<|hy_Assistant|>"
        )
        stop_tokens = ["<|hy_User|>", "<|hy_begin\u2581of\u2581sentence|>", "<|endoftext|>"]

    return raw_prompt, stop_tokens


# ---------------------------------------------------------------------------
# Парсинг відповіді MQM
# ---------------------------------------------------------------------------

def _parse_mqm_response(raw_text: str) -> dict:
    """Розпарсити відповідь LLM як MQM JSON.

    Стратегія:
      1. Спробувати json.loads(raw_text) прямо.
      2. Якщо не вдалось — спробувати витягнути JSON за допомогою regex
         (LLM іноді додає markdown fence або пояснення після JSON).
      3. Якщо і це не вдалось — повернути MQM_PARSE_FAILURE.

    Score clamp: якщо score поза [1,10] — відкидаємо до найближчої межі.
    Це запобігає помилкам downstream коли LLM повертає score=0 або score=11.

    Returns:
        dict з ключами: score (int|None), accept (bool), issues (list[str]).
        Ніколи не піднімає виняток.
    """
    if not raw_text or not raw_text.strip():
        return dict(MQM_PARSE_FAILURE)

    text = raw_text.strip()

    parsed = None

    # Спроба 1: прямий парсинг
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass

    # Спроба 2: витягнути перший JSON-об'єкт через regex
    if parsed is None:
        match = re.search(r'\{[^{}]*"score"[^{}]*\}', text, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(0))
            except (json.JSONDecodeError, ValueError):
                pass

    # Спроба 3: markdown fence ```json ... ```
    if parsed is None:
        fence_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
        if fence_match:
            try:
                parsed = json.loads(fence_match.group(1))
            except (json.JSONDecodeError, ValueError):
                pass

    if parsed is None or not isinstance(parsed, dict):
        return dict(MQM_PARSE_FAILURE)

    # Валідація та нормалізація score
    raw_score = parsed.get("score")
    if isinstance(raw_score, (int, float)) and not isinstance(raw_score, bool):
        # Clamp до [1, 10]
        score: Optional[int] = int(max(1, min(10, round(raw_score))))
    else:
        # score відсутній або некоректний тип → null
        score = None

    # Валідація accept
    raw_accept = parsed.get("accept")
    if isinstance(raw_accept, bool):
        accept = raw_accept
    else:
        # Якщо accept відсутній — визначаємо з score
        accept = (score is not None and score >= 7)

    # Валідація issues
    raw_issues = parsed.get("issues", [])
    if isinstance(raw_issues, list):
        issues = [str(i) for i in raw_issues if i]
    else:
        issues = []

    return {
        "score": score,
        "accept": accept,
        "issues": issues,
    }


# ---------------------------------------------------------------------------
# Визначення формату моделі (7B vs 1.8B) — аналогічно translate_text_hy_mt2
# ---------------------------------------------------------------------------

def _detect_model_format(base_url: str) -> tuple[bool, str]:
    """Визначити формат моделі (7B чи 1.8B) через /props ендпоінт.

    Returns:
        (is_7b_format, model_identifier) — model_identifier для поля mqm_model у флазі.
    """
    base = (
        base_url
        .replace("/v1/chat/completions", "")
        .replace("/completion", "")
        .rstrip("/")
    )
    try:
        props_resp = requests.get(f"{base}/props", timeout=10)
        if props_resp.status_code == 200:
            props = props_resp.json()
            tmpl = (
                props.get("chat_template", "")
                or props.get("model_alias", "")
                or props.get("model_path", "")
            )
            is_7b = "startoftext" in tmpl or "extra_0" in tmpl or "7b" in tmpl.lower()
            model_id = props.get("model_alias", "") or props.get("model_path", "") or "hy-mt2"
            # Скорочуємо до basename якщо шлях
            model_id = os.path.basename(model_id.rstrip("/")) or "hy-mt2"
            return is_7b, model_id
    except Exception:
        pass
    return False, "hy-mt2"


# ---------------------------------------------------------------------------
# Головна функція: mqm_review
# ---------------------------------------------------------------------------

def mqm_review(
    original: str,
    translated: str,
    api_url: str,
    source_lang: str = "ru",
    target_lang: str = "uk",
) -> dict:
    """Виконати MQM-оцінку перекладу через Hy-MT2 /completion.

    Це stateless виклик — аналогічний translate_text_hy_mt2(), але з іншим
    промптом, де модель грає роль MQM-критика, а не перекладача.

    Args:
        original:    Оригінальний текст (мова source_lang).
        translated:  Перекладений текст (мова target_lang).
        api_url:     URL до llama-server (/completion або /v1/chat/completions —
                     функція сама будує completion_url).
        source_lang: ISO-код мови оригіналу (default "ru").
        target_lang: ISO-код мови перекладу (default "uk").

    Returns:
        dict з ключами:
          "score"  — int (1-10) або None якщо parse failed
          "accept" — bool (True = переклад прийнятний)
          "issues" — list[str] (описи проблем)

    Fail-safe:
        Ніколи не піднімає виняток.  При будь-якій помилці (network, timeout,
        non-JSON відповідь) повертає MQM_PARSE_FAILURE:
          {"score": None, "accept": True, "issues": ["MQM review failed to parse"]}
        Логіка: best-effort якісний шар, НЕ gate — збій перевірки не блокує переклад.
    """
    source_lang_full = LANG_MAP.get(source_lang, source_lang.capitalize())
    target_lang_full = LANG_MAP.get(target_lang, target_lang.capitalize())

    try:
        is_7b_format, _ = _detect_model_format(api_url)
    except Exception:
        is_7b_format = False

    try:
        raw_prompt, stop_tokens = _build_mqm_prompt(
            original=original,
            translated=translated,
            source_lang_full=source_lang_full,
            target_lang_full=target_lang_full,
            is_7b_format=is_7b_format,
        )
    except Exception:
        return dict(MQM_PARSE_FAILURE)

    # Побудувати URL до /completion (аналогічно translate_text_hy_mt2)
    completion_url = api_url.replace("/v1/chat/completions", "/completion").rstrip("/")
    if not completion_url.endswith("/completion"):
        completion_url = completion_url.rstrip("/") + "/completion"

    headers = {"Content-Type": "application/json"}
    payload = {
        "prompt": raw_prompt,
        "temperature": 0.1,   # Низька температура для детерм. структурованої відповіді
        "top_p": 0.9,
        "top_k": 20,
        "repeat_penalty": 1.0,
        "n_predict": MQM_N_PREDICT,
        "stop": stop_tokens,
    }

    try:
        resp = requests.post(completion_url, headers=headers, json=payload, timeout=120)
        if resp.status_code != 200:
            return dict(MQM_PARSE_FAILURE)
        result = resp.json()
        raw_content = result.get("content", "").strip()
    except Exception:
        return dict(MQM_PARSE_FAILURE)

    return _parse_mqm_response(raw_content)


# ---------------------------------------------------------------------------
# Побудова MQM-флага (якісний запис для translation_quality_flags.json)
# ---------------------------------------------------------------------------

def build_mqm_flag(
    segment_id: str,
    original: str,
    translated: str,
    mqm_result: dict,
    source_lang: str = "ru",
    target_lang: str = "uk",
    mqm_model: str = "hy-mt2",
) -> dict:
    """Побудувати MQM-флаг для запису у translation_quality_flags.json.

    Args:
        segment_id:  Унікальний ідентифікатор сегмента (напр. "slug_para_042").
        original:    Оригінальний текст.
        translated:  Перекладений текст.
        mqm_result:  Результат mqm_review() — dict з score/accept/issues.
        source_lang: ISO-код мови оригіналу.
        target_lang: ISO-код мови перекладу.
        mqm_model:   Ідентифікатор моделі (для аудиту).

    Returns:
        dict з усіма полями для запису у JSON-файл.
    """
    score = mqm_result.get("score")
    accept = mqm_result.get("accept", True)
    issues = mqm_result.get("issues", [])

    if score is None:
        reason = "mqm_parse_failure"
    elif not accept:
        reason = "mqm_rejected"
    elif score < MQM_SCORE_WRITE_THRESHOLD:
        reason = "mqm_low_score"
    else:
        reason = "mqm_ok"

    return {
        "segment_id": segment_id,
        "original": original,
        "translated": translated,
        "source_lang": source_lang,
        "target_lang": target_lang,
        "score": score,
        "accept": accept,
        "issues": issues,
        "reason": reason,
        "mqm_model": mqm_model,
    }


# ---------------------------------------------------------------------------
# Запис у translation_quality_flags.json (аналог append_to_stress_queue)
# ---------------------------------------------------------------------------

def should_write_flag(mqm_result: dict) -> bool:
    """Визначити, чи варто записати флаг у файл якості.

    Правило: пишемо якщо:
      - score is None (parse failure — сегмент невідомої якості, потрібна ревізія)
      - accept == False
      - score < MQM_SCORE_WRITE_THRESHOLD (7)

    НЕ пишемо якщо score >= 7 AND accept == True — це «ок», зберігаємо місце.
    """
    score = mqm_result.get("score")
    accept = mqm_result.get("accept", True)

    if score is None:
        return True  # parse failure — завжди пишемо
    if not accept:
        return True
    if score < MQM_SCORE_WRITE_THRESHOLD:
        return True
    return False


def append_quality_flag(flag: dict, flags_path: str) -> None:
    """Атомарно додати MQM-флаг до translation_quality_flags.json.

    JSON-файл є масивом флагів (той самий патерн, що quality_flags.json у
    translate_manga.py і asr_stress_queue.json у asr_verify.py).

    Atomic write через tmp + os.replace — запобігає corruption при kill
    посеред запису (реальний failure mode, задокументований у asr_verify.py).

    Args:
        flag:       Dict, побудований через build_mqm_flag().
        flags_path: Шлях до файлу, напр. "books/<slug>/translation_quality_flags.json".
                    Директорія має існувати (створюється caller'ом або основним pipeline).
    """
    if os.path.exists(flags_path):
        with open(flags_path, "r", encoding="utf-8") as fh:
            try:
                flags = json.load(fh)
                if not isinstance(flags, list):
                    flags = []
            except (json.JSONDecodeError, ValueError):
                flags = []
    else:
        flags = []

    flags.append(flag)

    tmp_path = flags_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(flags, fh, ensure_ascii=False, indent=2)
    os.replace(tmp_path, flags_path)


# ---------------------------------------------------------------------------
# High-level: review_and_record  (зручний entry-point для майбутньої інтеграції)
# ---------------------------------------------------------------------------

def review_and_record(
    segment_id: str,
    original: str,
    translated: str,
    api_url: str,
    flags_path: str,
    source_lang: str = "ru",
    target_lang: str = "uk",
) -> dict:
    """MQM-огляд + запис флага у файл якості (якщо необхідно).

    Це планований entry-point для підключення до основного pipeline
    (після підтвердження Q, аналогічно TASK-86).

    Returns:
        mqm_result dict (завжди повертається, навіть при parse failure).
    """
    try:
        _, model_id = _detect_model_format(api_url)
    except Exception:
        model_id = "hy-mt2"

    mqm_result = mqm_review(
        original=original,
        translated=translated,
        api_url=api_url,
        source_lang=source_lang,
        target_lang=target_lang,
    )

    if should_write_flag(mqm_result):
        flag = build_mqm_flag(
            segment_id=segment_id,
            original=original,
            translated=translated,
            mqm_result=mqm_result,
            source_lang=source_lang,
            target_lang=target_lang,
            mqm_model=model_id,
        )
        append_quality_flag(flag, flags_path)

    return mqm_result
