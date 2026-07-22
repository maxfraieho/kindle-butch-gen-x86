#!/usr/bin/env python3
"""
test_mqm_review.py  —  TASK-88: Юніт-тести для common/mqm_review.py

Всі тести мокають requests.post (не потребують реального llama-server).
Запуск: python3 test_mqm_review.py

Очікуваний результат: всі тести PASS (exit code 0).

Сценарії (з УТОЧНЕННЯ пункт 5):
  (а) Нормальна відповідь з валідним JSON парситься коректно
  (б) LLM повертає щось що НЕ парситься як JSON — fail-safe (accept=True, не падає)
  (в) LLM повертає score поза діапазоном 1-10 — clamp до [1,10]
  (г) Реальний виклик build/append до translation_quality_flags.json — атомарний запис

Додаткові сценарії:
  - score=None (parse failure) → should_write_flag=True
  - score>=7 AND accept=True → should_write_flag=False  (не пишемо «ок»)
  - score<7 → should_write_flag=True
  - markdown fence у відповіді — парситься коректно
  - mqm_review() не піднімає виняток при network failure
"""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# Корінь проєкту в sys.path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from common.mqm_review import (
    MQM_PARSE_FAILURE,
    MQM_SCORE_WRITE_THRESHOLD,
    _build_mqm_prompt,
    _parse_mqm_response,
    append_quality_flag,
    build_mqm_flag,
    mqm_review,
    review_and_record,
    should_write_flag,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_props_response(is_7b: bool = False):
    """Мок /props відповіді для _detect_model_format."""
    tmpl = "<|startoftext|>..." if is_7b else "<|hy_begin▁of▁sentence|>..."
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "chat_template": tmpl,
        "model_alias": "hy-mt2-7b" if is_7b else "hy-mt2-1.8b",
        "model_path": "/models/hy-mt2.gguf",
    }
    return mock_resp


def _mock_completion_response(content: str):
    """Мок /completion відповіді."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"content": content}
    return mock_resp


# ---------------------------------------------------------------------------
# _parse_mqm_response
# ---------------------------------------------------------------------------

class TestParseMqmResponse(unittest.TestCase):
    """Тести парсингу відповіді LLM."""

    # --- (а) Нормальна валідна відповідь ---

    def test_valid_json_score_and_accept(self):
        """(а) Нормальна відповідь з валідним JSON парситься коректно."""
        raw = '{"score": 8, "accept": true, "issues": []}'
        result = _parse_mqm_response(raw)
        self.assertEqual(result["score"], 8)
        self.assertTrue(result["accept"])
        self.assertEqual(result["issues"], [])

    def test_valid_json_with_issues(self):
        """(а) Валідний JSON з кількома issues."""
        raw = '{"score": 4, "accept": false, "issues": ["Missing clause", "Wrong name"]}'
        result = _parse_mqm_response(raw)
        self.assertEqual(result["score"], 4)
        self.assertFalse(result["accept"])
        self.assertEqual(len(result["issues"]), 2)
        self.assertIn("Missing clause", result["issues"])

    def test_valid_json_with_whitespace(self):
        """(а) JSON з пробілами до/після — парситься."""
        raw = '\n  {"score": 9, "accept": true, "issues": []}\n'
        result = _parse_mqm_response(raw)
        self.assertEqual(result["score"], 9)

    # --- (б) Non-JSON відповідь — fail-safe ---

    def test_plain_text_not_json_returns_failsafe(self):
        """(б) Якщо LLM повертає не-JSON текст — fail-safe (accept=True, не падає)."""
        raw = "The translation looks good overall."
        result = _parse_mqm_response(raw)
        self.assertIsNone(result["score"])
        self.assertTrue(result["accept"])  # НЕ блокує переклад
        self.assertEqual(result["issues"], ["MQM review failed to parse"])

    def test_empty_string_returns_failsafe(self):
        """(б) Порожній рядок → fail-safe."""
        result = _parse_mqm_response("")
        self.assertIsNone(result["score"])
        self.assertTrue(result["accept"])

    def test_none_text_returns_failsafe(self):
        """(б) None → fail-safe (не падає)."""
        result = _parse_mqm_response(None)  # type: ignore
        self.assertIsNone(result["score"])
        self.assertTrue(result["accept"])

    def test_broken_json_returns_failsafe(self):
        """(б) Обірваний JSON → fail-safe."""
        raw = '{"score": 7, "accept": tr'  # обірвано
        result = _parse_mqm_response(raw)
        self.assertIsNone(result["score"])
        self.assertTrue(result["accept"])

    def test_json_array_not_object_returns_failsafe(self):
        """(б) JSON масив замість об'єкту → fail-safe."""
        raw = '[7, true, []]'
        result = _parse_mqm_response(raw)
        self.assertIsNone(result["score"])
        self.assertTrue(result["accept"])

    # --- (в) Score поза діапазоном 1-10 — clamp ---

    def test_score_above_10_clamped(self):
        """(в) score=15 → clamp до 10."""
        raw = '{"score": 15, "accept": true, "issues": []}'
        result = _parse_mqm_response(raw)
        self.assertEqual(result["score"], 10)

    def test_score_zero_clamped_to_1(self):
        """(в) score=0 → clamp до 1."""
        raw = '{"score": 0, "accept": false, "issues": ["total failure"]}'
        result = _parse_mqm_response(raw)
        self.assertEqual(result["score"], 1)

    def test_score_negative_clamped_to_1(self):
        """(в) score=-5 → clamp до 1."""
        raw = '{"score": -5, "accept": false, "issues": []}'
        result = _parse_mqm_response(raw)
        self.assertEqual(result["score"], 1)

    def test_score_float_rounded(self):
        """(в) score=7.8 (float) → round → 8."""
        raw = '{"score": 7.8, "accept": true, "issues": []}'
        result = _parse_mqm_response(raw)
        self.assertEqual(result["score"], 8)

    def test_score_string_returns_null(self):
        """(в) score="seven" (неправильний тип) → score=None."""
        raw = '{"score": "seven", "accept": true, "issues": []}'
        result = _parse_mqm_response(raw)
        self.assertIsNone(result["score"])

    # --- Embedded JSON у тексті ---

    def test_json_embedded_in_prose(self):
        """Regex витягує JSON з довшого тексту моделі."""
        raw = 'Here is my evaluation:\n{"score": 6, "accept": false, "issues": ["untranslated name"]}\nThank you.'
        result = _parse_mqm_response(raw)
        self.assertEqual(result["score"], 6)
        self.assertFalse(result["accept"])

    def test_json_in_markdown_fence(self):
        """Markdown fence ```json ... ``` парситься коректно."""
        raw = '```json\n{"score": 5, "accept": false, "issues": ["semantic distortion"]}\n```'
        result = _parse_mqm_response(raw)
        self.assertEqual(result["score"], 5)
        self.assertFalse(result["accept"])

    # --- accept відсутній — визначається зі score ---

    def test_missing_accept_inferred_from_score_high(self):
        """Якщо accept відсутній — accept=True якщо score >= 7."""
        raw = '{"score": 8, "issues": []}'
        result = _parse_mqm_response(raw)
        self.assertTrue(result["accept"])

    def test_missing_accept_inferred_from_score_low(self):
        """Якщо accept відсутній — accept=False якщо score < 7."""
        raw = '{"score": 5, "issues": ["problem"]}'
        result = _parse_mqm_response(raw)
        self.assertFalse(result["accept"])


# ---------------------------------------------------------------------------
# _build_mqm_prompt
# ---------------------------------------------------------------------------

class TestBuildMqmPrompt(unittest.TestCase):
    """Тести побудови raw_prompt."""

    def test_7b_format_uses_startoftext(self):
        prompt, stops = _build_mqm_prompt(
            "Привіт", "Hello", "Russian", "Ukrainian", is_7b_format=True
        )
        self.assertIn("<|startoftext|>", prompt)
        self.assertIn("<|extra_0|>", prompt)
        self.assertIn("<|eos|>", stops)

    def test_1_8b_format_uses_hy_tokens(self):
        prompt, stops = _build_mqm_prompt(
            "Привіт", "Hello", "Russian", "Ukrainian", is_7b_format=False
        )
        self.assertIn("<|hy_User|>", prompt)
        self.assertIn("<|hy_Assistant|>", prompt)
        self.assertIn("<|hy_User|>", stops)

    def test_prompt_contains_original_and_translated(self):
        orig = "Він вийшов на вулицю."
        trans = "Він вийшов на вулицю."
        prompt, _ = _build_mqm_prompt(orig, trans, "Russian", "Ukrainian", is_7b_format=False)
        self.assertIn(orig, prompt)
        self.assertIn(trans, prompt)

    def test_prompt_contains_json_instruction(self):
        prompt, _ = _build_mqm_prompt("a", "b", "Russian", "Ukrainian", is_7b_format=False)
        self.assertIn('"score"', prompt)
        self.assertIn('"accept"', prompt)
        self.assertIn('"issues"', prompt)

    def test_prompt_contains_lang_names(self):
        prompt, _ = _build_mqm_prompt("a", "b", "Russian", "Ukrainian", is_7b_format=False)
        self.assertIn("Russian", prompt)
        self.assertIn("Ukrainian", prompt)


# ---------------------------------------------------------------------------
# mqm_review (мок requests.post + requests.get)
# ---------------------------------------------------------------------------

class TestMqmReview(unittest.TestCase):
    """Інтеграційні тести mqm_review() з мокованим HTTP."""

    # --- (а) Нормальна відповідь ---

    @patch("common.mqm_review.requests.post")
    @patch("common.mqm_review.requests.get")
    def test_valid_response_parsed(self, mock_get, mock_post):
        """(а) Нормальна відповідь з валідним JSON парситься і повертається."""
        mock_get.return_value = _mock_props_response(is_7b=False)
        mock_post.return_value = _mock_completion_response(
            '{"score": 8, "accept": true, "issues": []}'
        )
        result = mqm_review("Тест", "Test", "http://localhost:8080/completion")
        self.assertEqual(result["score"], 8)
        self.assertTrue(result["accept"])
        self.assertEqual(result["issues"], [])

    @patch("common.mqm_review.requests.post")
    @patch("common.mqm_review.requests.get")
    def test_valid_response_7b_format(self, mock_get, mock_post):
        """(а) 7B формат — промпт використовує startoftext токени, результат коректний."""
        mock_get.return_value = _mock_props_response(is_7b=True)
        mock_post.return_value = _mock_completion_response(
            '{"score": 6, "accept": false, "issues": ["missing clause"]}'
        )
        result = mqm_review("Тест", "Test", "http://localhost:8080/completion")
        self.assertEqual(result["score"], 6)
        self.assertFalse(result["accept"])
        # Перевіряємо що POST виклик відбувся
        mock_post.assert_called_once()
        # Перевіряємо що у промпті є 7B токен
        call_payload = mock_post.call_args[1]["json"]
        self.assertIn("<|startoftext|>", call_payload["prompt"])

    # --- (б) Non-JSON відповідь — fail-safe ---

    @patch("common.mqm_review.requests.post")
    @patch("common.mqm_review.requests.get")
    def test_non_json_response_failsafe(self, mock_get, mock_post):
        """(б) LLM повертає не-JSON → fail-safe (accept=True, не падає)."""
        mock_get.return_value = _mock_props_response(is_7b=False)
        mock_post.return_value = _mock_completion_response(
            "The translation is acceptable."
        )
        result = mqm_review("Тест", "Test", "http://localhost:8080/completion")
        self.assertIsNone(result["score"])
        self.assertTrue(result["accept"])  # НЕ блокує
        self.assertEqual(result["issues"], ["MQM review failed to parse"])

    @patch("common.mqm_review.requests.post")
    @patch("common.mqm_review.requests.get")
    def test_network_failure_failsafe(self, mock_get, mock_post):
        """(б) Network error → fail-safe (не падає)."""
        mock_get.side_effect = Exception("Connection refused")
        mock_post.side_effect = Exception("Connection refused")
        result = mqm_review("Тест", "Test", "http://localhost:8080/completion")
        self.assertIsNone(result["score"])
        self.assertTrue(result["accept"])

    @patch("common.mqm_review.requests.post")
    @patch("common.mqm_review.requests.get")
    def test_http_500_failsafe(self, mock_get, mock_post):
        """(б) HTTP 500 → fail-safe."""
        mock_get.return_value = _mock_props_response(is_7b=False)
        error_resp = MagicMock()
        error_resp.status_code = 500
        mock_post.return_value = error_resp
        result = mqm_review("Тест", "Test", "http://localhost:8080/completion")
        self.assertIsNone(result["score"])
        self.assertTrue(result["accept"])

    # --- (в) Score поза діапазоном ---

    @patch("common.mqm_review.requests.post")
    @patch("common.mqm_review.requests.get")
    def test_score_out_of_range_clamped(self, mock_get, mock_post):
        """(в) LLM повертає score=15 → clamp до 10, НЕ кидає виняток."""
        mock_get.return_value = _mock_props_response(is_7b=False)
        mock_post.return_value = _mock_completion_response(
            '{"score": 15, "accept": true, "issues": []}'
        )
        result = mqm_review("Тест", "Test", "http://localhost:8080/completion")
        self.assertEqual(result["score"], 10)
        # Clamp до 10, accept лишається True
        self.assertTrue(result["accept"])

    @patch("common.mqm_review.requests.post")
    @patch("common.mqm_review.requests.get")
    def test_score_zero_clamped(self, mock_get, mock_post):
        """(в) score=0 → clamp до 1."""
        mock_get.return_value = _mock_props_response(is_7b=False)
        mock_post.return_value = _mock_completion_response(
            '{"score": 0, "accept": false, "issues": ["complete failure"]}'
        )
        result = mqm_review("Тест", "Test", "http://localhost:8080/completion")
        self.assertEqual(result["score"], 1)

    # --- Правильна побудова completion_url ---

    @patch("common.mqm_review.requests.post")
    @patch("common.mqm_review.requests.get")
    def test_chat_completions_url_converted(self, mock_get, mock_post):
        """URL /v1/chat/completions перетворюється на /completion."""
        mock_get.return_value = _mock_props_response(is_7b=False)
        mock_post.return_value = _mock_completion_response(
            '{"score": 9, "accept": true, "issues": []}'
        )
        mqm_review("a", "b", "http://localhost:8080/v1/chat/completions")
        called_url = mock_post.call_args[0][0]
        self.assertTrue(called_url.endswith("/completion"), f"URL: {called_url}")
        self.assertNotIn("chat/completions", called_url)


# ---------------------------------------------------------------------------
# should_write_flag
# ---------------------------------------------------------------------------

class TestShouldWriteFlag(unittest.TestCase):
    """Тести логіки порогу запису у файл."""

    def test_parse_failure_always_written(self):
        """score=None (parse failure) → пишемо завжди."""
        self.assertTrue(should_write_flag({"score": None, "accept": True, "issues": []}))

    def test_accept_false_always_written(self):
        """accept=False → пишемо незалежно від score."""
        self.assertTrue(should_write_flag({"score": 8, "accept": False, "issues": []}))

    def test_low_score_written(self):
        """score < 7 → пишемо."""
        self.assertTrue(should_write_flag({"score": 6, "accept": True, "issues": []}))
        self.assertTrue(should_write_flag({"score": 1, "accept": True, "issues": []}))

    def test_score_at_threshold_not_written(self):
        """score == 7 AND accept=True → НЕ пишемо (поріг включає 7)."""
        self.assertFalse(should_write_flag({"score": 7, "accept": True, "issues": []}))

    def test_high_score_accept_true_not_written(self):
        """score >= 7 AND accept=True → НЕ пишемо."""
        self.assertFalse(should_write_flag({"score": 9, "accept": True, "issues": []}))
        self.assertFalse(should_write_flag({"score": 10, "accept": True, "issues": []}))


# ---------------------------------------------------------------------------
# (г) append_quality_flag — атомарний запис у файл
# ---------------------------------------------------------------------------

class TestAppendQualityFlag(unittest.TestCase):
    """(г) Тести build/append до translation_quality_flags.json."""

    def _make_flag(self, segment_id="para_001", score=4, accept=False):
        return build_mqm_flag(
            segment_id=segment_id,
            original="Він вийшов з кімнати.",
            translated="He exited the room.",
            mqm_result={"score": score, "accept": accept, "issues": ["semantic shift"]},
            source_lang="ru",
            target_lang="uk",
            mqm_model="hy-mt2-1.8b",
        )

    def test_creates_file_if_not_exists(self):
        """(г) Файл створюється якщо не існував."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "translation_quality_flags.json")
            flag = self._make_flag()
            append_quality_flag(flag, path)
            self.assertTrue(os.path.exists(path))

    def test_file_is_valid_json_array(self):
        """(г) Файл є валідним JSON-масивом після запису."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "translation_quality_flags.json")
            flag = self._make_flag()
            append_quality_flag(flag, path)
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertIsInstance(data, list)
            self.assertEqual(len(data), 1)

    def test_multiple_appends_accumulate(self):
        """(г) Декілька append → масив зростає."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "translation_quality_flags.json")
            for i in range(3):
                flag = self._make_flag(segment_id=f"para_{i:03d}")
                append_quality_flag(flag, path)
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertEqual(len(data), 3)
            self.assertEqual(data[2]["segment_id"], "para_002")

    def test_atomic_write_no_tmp_left(self):
        """(г) Після запису .tmp файл не лишається."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "translation_quality_flags.json")
            flag = self._make_flag()
            append_quality_flag(flag, path)
            self.assertFalse(os.path.exists(path + ".tmp"))

    def test_flag_fields_complete(self):
        """(г) Флаг містить усі обов'язкові поля."""
        flag = self._make_flag(segment_id="para_042", score=3, accept=False)
        required_fields = {
            "segment_id", "original", "translated", "source_lang",
            "target_lang", "score", "accept", "issues", "reason", "mqm_model",
        }
        self.assertEqual(required_fields, set(flag.keys()))
        self.assertEqual(flag["segment_id"], "para_042")
        self.assertEqual(flag["score"], 3)
        self.assertFalse(flag["accept"])
        self.assertEqual(flag["reason"], "mqm_rejected")

    def test_parse_failure_flag_reason(self):
        """(г) Parse failure → reason='mqm_parse_failure'."""
        flag = build_mqm_flag(
            segment_id="para_err",
            original="a",
            translated="b",
            mqm_result={"score": None, "accept": True, "issues": ["MQM review failed to parse"]},
        )
        self.assertEqual(flag["reason"], "mqm_parse_failure")

    def test_ok_flag_reason(self):
        """(г) score>=7 AND accept=True → reason='mqm_ok'."""
        flag = build_mqm_flag(
            segment_id="para_ok",
            original="a",
            translated="b",
            mqm_result={"score": 9, "accept": True, "issues": []},
        )
        self.assertEqual(flag["reason"], "mqm_ok")

    def test_low_score_flag_reason(self):
        """(г) score<7 AND accept=True → reason='mqm_low_score'."""
        flag = build_mqm_flag(
            segment_id="para_low",
            original="a",
            translated="b",
            mqm_result={"score": 5, "accept": True, "issues": ["minor issue"]},
        )
        self.assertEqual(flag["reason"], "mqm_low_score")

    def test_existing_corrupt_json_recovered(self):
        """Якщо файл є але corrupt JSON — заміняється чистим масивом."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "translation_quality_flags.json")
            with open(path, "w") as fh:
                fh.write("NOT JSON {{{")
            flag = self._make_flag()
            # Не має падати
            append_quality_flag(flag, path)
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertEqual(len(data), 1)


# ---------------------------------------------------------------------------
# review_and_record (інтеграційний)
# ---------------------------------------------------------------------------

class TestReviewAndRecord(unittest.TestCase):
    """Тест high-level review_and_record()."""

    @patch("common.mqm_review.requests.post")
    @patch("common.mqm_review.requests.get")
    def test_low_score_written_to_file(self, mock_get, mock_post):
        """Low score → флаг записується у файл."""
        mock_get.return_value = _mock_props_response(is_7b=False)
        mock_post.return_value = _mock_completion_response(
            '{"score": 4, "accept": false, "issues": ["information omission"]}'
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "translation_quality_flags.json")
            result = review_and_record(
                segment_id="para_001",
                original="Він відкрив скриньку.",
                translated="He opened it.",
                api_url="http://localhost:8080/completion",
                flags_path=path,
            )
            self.assertEqual(result["score"], 4)
            self.assertTrue(os.path.exists(path))
            with open(path) as fh:
                flags = json.load(fh)
            self.assertEqual(len(flags), 1)
            self.assertIn("information omission", flags[0]["issues"])

    @patch("common.mqm_review.requests.post")
    @patch("common.mqm_review.requests.get")
    def test_high_score_not_written(self, mock_get, mock_post):
        """High score (>=7) AND accept=True → НЕ пишеться у файл."""
        mock_get.return_value = _mock_props_response(is_7b=False)
        mock_post.return_value = _mock_completion_response(
            '{"score": 9, "accept": true, "issues": []}'
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "translation_quality_flags.json")
            result = review_and_record(
                segment_id="para_002",
                original="Тест",
                translated="Test",
                api_url="http://localhost:8080/completion",
                flags_path=path,
            )
            self.assertEqual(result["score"], 9)
            # Файл НЕ створювався — нема що писати
            self.assertFalse(os.path.exists(path))

    @patch("common.mqm_review.requests.post")
    @patch("common.mqm_review.requests.get")
    def test_parse_failure_written_with_failsafe(self, mock_get, mock_post):
        """Parse failure → флаг записується (score=None, accept=True)."""
        mock_get.return_value = _mock_props_response(is_7b=False)
        mock_post.return_value = _mock_completion_response("not a json response at all")
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "translation_quality_flags.json")
            result = review_and_record(
                segment_id="para_err",
                original="Тест",
                translated="Test",
                api_url="http://localhost:8080/completion",
                flags_path=path,
            )
            # Fail-safe: accept=True (не блокує)
            self.assertTrue(result["accept"])
            self.assertIsNone(result["score"])
            # Але флаг записано (щоб знати що сталась помилка MQM)
            self.assertTrue(os.path.exists(path))
            with open(path) as fh:
                flags = json.load(fh)
            self.assertEqual(flags[0]["reason"], "mqm_parse_failure")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Запуск з детальним виводом
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    test_classes = [
        TestParseMqmResponse,
        TestBuildMqmPrompt,
        TestMqmReview,
        TestShouldWriteFlag,
        TestAppendQualityFlag,
        TestReviewAndRecord,
    ]

    for cls in test_classes:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    if not result.wasSuccessful():
        sys.exit(1)
