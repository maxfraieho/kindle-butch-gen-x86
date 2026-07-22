#!/usr/bin/env python3
"""
test_asr_verify.py  —  TASK-86: Юніт-тести для common/asr_verify.py

Всі тести мокають subprocess.run (не потребують реального whisper-cli).
Запуск: python3 test_asr_verify.py

Очікуваний результат: усі тести PASS (exit code 0).
"""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# Додаємо корінь проєкту в sys.path щоб дозволити `from common.asr_verify import ...`
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from common.asr_verify import (
    append_to_stress_queue,
    build_mismatch_flag,
    char_error_rate,
    levenshtein_distance,
    transcribe,
    verify_chunk,
)


# ---------------------------------------------------------------------------
# Левенштейн
# ---------------------------------------------------------------------------

class TestLevenshteinDistance(unittest.TestCase):

    def test_identical_strings(self):
        self.assertEqual(levenshtein_distance("hello", "hello"), 0)

    def test_empty_strings(self):
        self.assertEqual(levenshtein_distance("", ""), 0)

    def test_one_empty(self):
        self.assertEqual(levenshtein_distance("abc", ""), 3)
        self.assertEqual(levenshtein_distance("", "abc"), 3)

    def test_single_insert(self):
        self.assertEqual(levenshtein_distance("cat", "cats"), 1)

    def test_single_delete(self):
        self.assertEqual(levenshtein_distance("cats", "cat"), 1)

    def test_single_substitution(self):
        self.assertEqual(levenshtein_distance("cat", "bat"), 1)

    def test_kitten_sitting(self):
        # Класичний приклад: kitten → sitting = 3
        self.assertEqual(levenshtein_distance("kitten", "sitting"), 3)

    def test_ukrainian_identical(self):
        # Рядки з наголосами — мають вважатися ідентичними
        self.assertEqual(levenshtein_distance("замо́к", "замо́к"), 0)

    def test_ukrainian_stress_mismatch(self):
        # "замо́к" (lock) vs "за́мок" (castle) — 2 символи відрізняються позицією наголосу
        dist = levenshtein_distance("замок", "за́мок")
        self.assertGreater(dist, 0)

    def test_symmetry(self):
        a, b = "тест", "текст"
        self.assertEqual(levenshtein_distance(a, b), levenshtein_distance(b, a))


# ---------------------------------------------------------------------------
# CER
# ---------------------------------------------------------------------------

class TestCharErrorRate(unittest.TestCase):

    def test_perfect_match(self):
        self.assertAlmostEqual(char_error_rate("hello", "hello"), 0.0)

    def test_zero_ref_doesnt_divide_by_zero(self):
        # ref="" → max(len(""), 1) = 1 → не ділення на нуль
        cer = char_error_rate("", "abc")
        self.assertEqual(cer, 3.0)

    def test_cer_formula(self):
        # levenshtein("abc", "abd") = 1, len("abc") = 3  →  CER = 1/3
        self.assertAlmostEqual(char_error_rate("abc", "abd"), 1 / 3)

    def test_cer_above_1(self):
        # ASR додає купу зайвих символів — CER > 1 можливо
        cer = char_error_rate("hi", "hello world this is long hallucination")
        self.assertGreater(cer, 1.0)


# ---------------------------------------------------------------------------
# build_mismatch_flag
# ---------------------------------------------------------------------------

class TestBuildMismatchFlag(unittest.TestCase):

    def _make_flag(self, original, transcribed, threshold=0.15):
        return build_mismatch_flag(
            chunk_id="book1_chunk01",
            audio_path="/tmp/chunk01.wav",
            original_text=original,
            transcribed_text=transcribed,
            cer_threshold=threshold,
            asr_backend="mock",
            asr_model="whisper-small",
        )

    def test_perfect_match_no_mismatch(self):
        flag = self._make_flag("тест тест тест", "тест тест тест")
        self.assertFalse(flag["mismatch"])
        self.assertEqual(flag["reason"], "asr_ok")
        self.assertEqual(flag["levenshtein_distance"], 0)
        self.assertAlmostEqual(flag["char_error_rate"], 0.0)

    def test_high_cer_triggers_mismatch(self):
        flag = self._make_flag("Він пішов додому.", "Він пішов до лісу і не повернувся ніколи.")
        self.assertTrue(flag["mismatch"])
        self.assertEqual(flag["reason"], "asr_mismatch")
        self.assertGreater(flag["char_error_rate"], 0.15)

    def test_just_below_threshold(self):
        # 1 символ різниця на рядку довжиною 100 — CER < 0.15
        ref = "а" * 100
        hyp = "а" * 99 + "б"  # 1 substitution
        flag = self._make_flag(ref, hyp, threshold=0.15)
        self.assertFalse(flag["mismatch"])  # 1/100 = 0.01 < 0.15

    def test_required_fields_present(self):
        flag = self._make_flag("test", "test")
        required = [
            "chunk_id", "audio_path", "original_text", "transcribed_text",
            "levenshtein_distance", "char_error_rate", "mismatch", "reason",
            "cer_threshold", "asr_backend", "asr_model",
        ]
        for field in required:
            self.assertIn(field, flag, f"Missing field: {field}")

    def test_metadata_preserved(self):
        flag = self._make_flag("text", "text")
        self.assertEqual(flag["chunk_id"], "book1_chunk01")
        self.assertEqual(flag["audio_path"], "/tmp/chunk01.wav")
        self.assertEqual(flag["asr_backend"], "mock")
        self.assertEqual(flag["asr_model"], "whisper-small")
        self.assertEqual(flag["cer_threshold"], 0.15)

    def test_flag_is_json_serializable(self):
        flag = self._make_flag("Яблуко впало.", "Яблуко вдало.")
        # Має серіалізуватись без помилок
        serialized = json.dumps(flag, ensure_ascii=False)
        restored = json.loads(serialized)
        self.assertEqual(restored["chunk_id"], flag["chunk_id"])


# ---------------------------------------------------------------------------
# transcribe (mocked sherpa-onnx)
# ---------------------------------------------------------------------------

class TestTranscribe(unittest.TestCase):

    def setUp(self):
        # Clear cache before each test
        import common.asr_verify
        common.asr_verify._recognizer = None

        self.mock_numpy = MagicMock()
        self.mock_sherpa = MagicMock()
        self.mock_wave = MagicMock()

        self.dict_patcher = patch.dict("sys.modules", {
            "numpy": self.mock_numpy,
            "sherpa_onnx": self.mock_sherpa
        })
        self.dict_patcher.start()

        self.wave_patcher = patch("wave.open", self.mock_wave)
        self.wave_patcher.start()

    def tearDown(self):
        self.dict_patcher.stop()
        self.wave_patcher.stop()

    def _fake_dir(self):
        tmp = tempfile.TemporaryDirectory()
        for name in ["encoder.onnx", "decoder.onnx", "tokens.txt"]:
            with open(os.path.join(tmp.name, name), "w") as f:
                f.write("")
        return tmp

    def _fake_wav_path(self):
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
        tmp.close()
        return tmp.name

    def test_transcribe_success(self):
        tmpdir = self._fake_dir()
        wav_path = self._fake_wav_path()
        try:
            # Set up wave mock
            mock_file = MagicMock()
            mock_file.getnchannels.return_value = 1
            mock_file.getsampwidth.return_value = 2
            mock_file.getframerate.return_value = 16000
            mock_file.getnframes.return_value = 16000
            mock_file.readframes.return_value = b"\x00" * 32000
            self.mock_wave.return_value.__enter__.return_value = mock_file

            # Set up numpy mock
            self.mock_numpy.frombuffer.return_value = MagicMock()

            # Set up sherpa_onnx mock
            mock_recognizer = MagicMock()
            mock_stream = MagicMock()
            mock_stream.result.text = "Hello world"
            mock_recognizer.create_stream.return_value = mock_stream
            self.mock_sherpa.OfflineRecognizer.from_whisper.return_value = mock_recognizer

            result = transcribe(
                audio_path=wav_path,
                model_dir=tmpdir.name,
                language="uk",
                num_threads=4
            )

            self.assertEqual(result, "Hello world")
            self.mock_sherpa.OfflineRecognizer.from_whisper.assert_called_once_with(
                encoder=os.path.join(tmpdir.name, "encoder.onnx"),
                decoder=os.path.join(tmpdir.name, "decoder.onnx"),
                tokens=os.path.join(tmpdir.name, "tokens.txt"),
                num_threads=4,
                language="uk",
                task="transcribe"
            )
        finally:
            tmpdir.cleanup()
            os.unlink(wav_path)

    def test_transcribe_raises_filenotfound_for_missing_model_dir(self):
        with self.assertRaises(FileNotFoundError) as ctx:
            transcribe(
                audio_path="/tmp/some.wav",
                model_dir="/nonexistent/model_dir",
            )
        self.assertIn("Whisper model directory not found", str(ctx.exception))

    def test_transcribe_raises_filenotfound_for_missing_audio(self):
        tmpdir = self._fake_dir()
        try:
            with self.assertRaises(FileNotFoundError) as ctx:
                transcribe(
                    audio_path="/nonexistent/audio.wav",
                    model_dir=tmpdir.name,
                )
            self.assertIn("Audio file not found", str(ctx.exception))
        finally:
            tmpdir.cleanup()


# ---------------------------------------------------------------------------
# append_to_stress_queue
# ---------------------------------------------------------------------------

class TestAppendToStressQueue(unittest.TestCase):

    def test_creates_new_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            queue_path = os.path.join(tmpdir, "asr_stress_queue.json")
            flag = build_mismatch_flag(
                chunk_id="c1", audio_path="/tmp/c1.wav",
                original_text="hello", transcribed_text="hello",
            )
            append_to_stress_queue(flag, queue_path)
            self.assertTrue(os.path.exists(queue_path))
            with open(queue_path, encoding="utf-8") as f:
                data = json.load(f)
            self.assertIsInstance(data, list)
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]["chunk_id"], "c1")

    def test_appends_to_existing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            queue_path = os.path.join(tmpdir, "asr_stress_queue.json")
            flag1 = build_mismatch_flag(
                chunk_id="c1", audio_path="/tmp/c1.wav",
                original_text="a", transcribed_text="a",
            )
            flag2 = build_mismatch_flag(
                chunk_id="c2", audio_path="/tmp/c2.wav",
                original_text="b", transcribed_text="bbb",
            )
            append_to_stress_queue(flag1, queue_path)
            append_to_stress_queue(flag2, queue_path)
            with open(queue_path, encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(len(data), 2)
            self.assertEqual(data[1]["chunk_id"], "c2")

    def test_handles_corrupted_file_gracefully(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            queue_path = os.path.join(tmpdir, "asr_stress_queue.json")
            with open(queue_path, "w") as f:
                f.write("THIS IS NOT JSON }{")
            flag = build_mismatch_flag(
                chunk_id="c1", audio_path="/tmp/c1.wav",
                original_text="x", transcribed_text="x",
            )
            append_to_stress_queue(flag, queue_path)
            with open(queue_path, encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(len(data), 1)

    def test_ukrainian_text_preserved(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            queue_path = os.path.join(tmpdir, "queue.json")
            flag = build_mismatch_flag(
                chunk_id="uk1", audio_path="/tmp/uk.wav",
                original_text="Він пішов додому.",
                transcribed_text="Він пішов до лісу.",
            )
            append_to_stress_queue(flag, queue_path)
            with open(queue_path, encoding="utf-8") as f:
                raw = f.read()
            self.assertIn("Він пішов додому.", raw)


# ---------------------------------------------------------------------------
# verify_chunk (high-level, mocked)
# ---------------------------------------------------------------------------

class TestVerifyChunk(unittest.TestCase):

    def test_verify_chunk_ok(self):
        """verify_chunk returns flag with mismatch=False on good transcription."""
        wav_mock = "/fake/audio.wav"

        with patch("common.asr_verify.transcribe", return_value="він пішов додому") as mock_t:
            flag = verify_chunk(
                chunk_id="book1_ch01",
                audio_path=wav_mock,
                original_text="Він пішов додому.",
                model_dir="/models/whisper-small-onnx",
                cer_threshold=0.15,
            )
            mock_t.assert_called_once()
        self.assertFalse(flag["mismatch"])
        self.assertEqual(flag["asr_backend"], "sherpa-onnx")

    def test_verify_chunk_on_transcription_error(self):
        """verify_chunk returns mismatch=True (fail-safe) on ASR exception."""
        with patch("common.asr_verify.transcribe", side_effect=FileNotFoundError("no model files")):
            flag = verify_chunk(
                chunk_id="book1_ch02",
                audio_path="/fake/audio.wav",
                original_text="Він пішов додому.",
                model_dir="/nonexistent/model",
            )
        self.assertTrue(flag["mismatch"])
        self.assertIn("<error:", flag["transcribed_text"])
        self.assertEqual(flag["asr_backend"], "sherpa-onnx-error")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
