import json
import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

from common.utils import translate_segment_with_retry, _maybe_mqm_review
from common.text_protect import PlaceholderManager

class TestMQMPipelineIntegration(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.config_path = os.path.join(self.tmpdir, "config.json")
        self.flags_path = os.path.join(self.tmpdir, "translation_quality_flags.json")
        self.pm = PlaceholderManager()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _set_mqm_enabled(self, enabled: bool):
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump({"enable_mqm_review": enabled}, f)

    @patch("common.utils.translate_text")
    def test_mqm_disabled_by_default_no_overhead(self, mock_translate):
        """When enable_mqm_review is False or missing, MQM review is skipped instantly."""
        mock_translate.return_value = "Це тестове речення."
        self._set_mqm_enabled(False)

        start = time.time()
        res = translate_segment_with_retry(
            segment="Это тестовое предложение.",
            pm=self.pm,
            api_url="http://127.0.0.1:8081/v1/chat/completions",
            target_lang="uk",
            source_lang="ru",
            book_dir=self.tmpdir
        )
        elapsed = time.time() - start

        self.assertEqual(res, "Це тестове речення.")
        self.assertFalse(os.path.exists(self.flags_path))
        self.assertLess(elapsed, 0.5)

    @patch("common.utils.translate_text")
    @patch("common.mqm_review.requests.post")
    @patch("common.mqm_review.requests.get")
    def test_mqm_enabled_invokes_mqm_and_writes_low_score_flag(self, mock_get, mock_post, mock_translate):
        """When enable_mqm_review is True, MQM review runs and logs low-score flags atomically."""
        mock_translate.return_value = "Це некоректний переклад."
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"default_generation_settings": {}}
        
        # Simulated MQM response returning score=4 (below threshold 7)
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {
            "content": '{"score": 4, "accept": false, "issues": ["Severe mistranslation"]}'
        }

        self._set_mqm_enabled(True)

        res = translate_segment_with_retry(
            segment="Это неверный перевод.",
            pm=self.pm,
            api_url="http://127.0.0.1:8081/v1/chat/completions",
            target_lang="uk",
            source_lang="ru",
            book_dir=self.tmpdir
        )

        self.assertEqual(res, "Це некоректний переклад.")
        self.assertTrue(os.path.exists(self.flags_path))
        self.assertFalse(os.path.exists(self.flags_path + ".tmp"))

        with open(self.flags_path, "r", encoding="utf-8") as f:
            flags = json.load(f)

        self.assertEqual(len(flags), 1)
        self.assertEqual(flags[0]["score"], 4)
        self.assertFalse(flags[0]["accept"])
        self.assertEqual(flags[0]["reason"], "mqm_rejected")

    @patch("common.utils.translate_text")
    @patch("common.mqm_review.requests.post")
    def test_mqm_failsafe_on_network_timeout(self, mock_post, mock_translate):
        """When MQM review times out or throws exception, translation still succeeds uninterrupted."""
        mock_translate.return_value = "Успішний переклад."
        mock_post.side_effect = Exception("Connection timed out (120s)")

        self._set_mqm_enabled(True)

        start = time.time()
        res = translate_segment_with_retry(
            segment="Успешный перевод.",
            pm=self.pm,
            api_url="http://127.0.0.1:8081/v1/chat/completions",
            target_lang="uk",
            source_lang="ru",
            book_dir=self.tmpdir
        )
        elapsed = time.time() - start

        # Translation MUST NOT be lost or blocked
        self.assertEqual(res, "Успішний переклад.")
        self.assertTrue(os.path.exists(self.flags_path))
        
        # Should record parse_failure flag safely
        with open(self.flags_path, "r", encoding="utf-8") as f:
            flags = json.load(f)
        self.assertEqual(len(flags), 1)
        self.assertEqual(flags[0]["reason"], "mqm_parse_failure")
        self.assertIsNone(flags[0]["score"])
        self.assertTrue(flags[0]["accept"])

    @patch("common.utils.translate_text")
    @patch("common.mqm_review.requests.post")
    @patch("common.mqm_review.requests.get")
    def test_timing_benchmark_5_segments(self, mock_get, mock_post, mock_translate):
        """Benchmark 5 segments with MQM disabled vs enabled (simulated LLM latency)."""
        segments = [
            ("Первое предложение для теста.", "Перше речення для тесту."),
            ("Второе предложение для проверки времени.", "Друге речення для перевірки часу."),
            ("Третье длинное предложение с описанием детализации текста.", "Третє довге речення з описом деталізації тексту."),
            ("Четвертый фрагмент текста для оценки производительности.", "Четвертий фрагмент тексту для оцінки продуктивності."),
            ("Пятый заключительный фрагмент набора данных.", "П'ятий заключний фрагмент набору даних.")
        ]

        mock_translate.side_effect = lambda seg, *a, **kw: [s[1] for s in segments if s[0] == seg][0]

        # 1. Disabled MQM Benchmark
        self._set_mqm_enabled(False)
        start_disabled = time.time()
        for seg, expected in segments:
            translate_segment_with_retry(
                segment=seg, pm=self.pm, api_url="http://127.0.0.1:8081/v1/chat/completions",
                target_lang="uk", source_lang="ru", book_dir=self.tmpdir
            )
        time_disabled = time.time() - start_disabled

        # 2. Enabled MQM Benchmark (simulate 0.1s review latency per segment)
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {}
        def mock_mqm_post(*args, **kwargs):
            time.sleep(0.1) # Simulate 100ms MQM reflection latency
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"content": '{"score": 9, "accept": true, "issues": []}'}
            return resp
        mock_post.side_effect = mock_mqm_post

        self._set_mqm_enabled(True)
        start_enabled = time.time()
        for seg, expected in segments:
            translate_segment_with_retry(
                segment=seg, pm=self.pm, api_url="http://127.0.0.1:8081/v1/chat/completions",
                target_lang="uk", source_lang="ru", book_dir=self.tmpdir
            )
        time_enabled = time.time() - start_enabled

        delta = time_enabled - time_disabled
        print(f"\n--- MQM TIMING BENCHMARK (5 SEGMENTS) ---")
        print(f"MQM Disabled: {time_disabled:.4f}s (Avg per seg: {time_disabled/5:.4f}s)")
        print(f"MQM Enabled:  {time_enabled:.4f}s (Avg per seg: {time_enabled/5:.4f}s)")
        print(f"Time Delta:   +{delta:.4f}s (+{delta/5:.4f}s per segment)")

if __name__ == "__main__":
    unittest.main()
