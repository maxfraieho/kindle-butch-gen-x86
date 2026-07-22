import unittest
from unittest.mock import patch, MagicMock
import os
import tempfile
import json
import shutil

# Ensure we import status_helper
from kbg_web.status_helper import calculate_progress

class TestCalculateProgress(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        
    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    @patch('kbg_web.status_helper.resolve_book_paths')
    def test_calculate_progress_audiobook_true(self, mock_resolve):
        # Setup mock paths
        book_dir = os.path.join(self.temp_dir, "books", "testbook")
        os.makedirs(book_dir, exist_ok=True)
        config_path = os.path.join(book_dir, "config.json")
        translated_dir = os.path.join(book_dir, "translated")
        os.makedirs(translated_dir, exist_ok=True)
        cache_dir = os.path.join(book_dir, "cache")
        os.makedirs(cache_dir, exist_ok=True)
        batches_dir = os.path.join(book_dir, "batches")
        os.makedirs(batches_dir, exist_ok=True)
        audio_dir = os.path.join(book_dir, "audio")
        os.makedirs(audio_dir, exist_ok=True)
        
        mock_resolve.return_value = {
            "book_dir": book_dir,
            "config_path": config_path,
            "cache_dir": cache_dir,
            "translate_cache": os.path.join(cache_dir, "translate_cache.json"),
            "batches_dir": batches_dir,
            "translated_dir": translated_dir,
            "audio_dir": audio_dir,
            "target_lang": "uk",
            "source_lang": "en"
        }
        
        # Write config.json
        config_data = {
            "is_manga": False,
            "generate_audiobook": True,
            "target_lang": "uk",
            "source_lang": "en"
        }
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config_data, f)

        # Create merged_translated file to satisfy translation_percent check
        merged_translated = os.path.join(translated_dir, "merged_translated_uk.md")
        with open(merged_translated, "w") as f:
            f.write("Some text")

        # Call calculate_progress
        res = calculate_progress("testbook")
        self.assertEqual(res["marker_percent"], 100.0)
        self.assertEqual(res["translation_percent"], 100.0)
        self.assertEqual(res["stress_percent"], 0.0)
        self.assertEqual(res["tts_percent"], 0.0)
        
        # Fails because overall_percent is not implemented yet
        self.assertIn("overall_percent", res)
        self.assertEqual(res["overall_percent"], 50.0)

    @patch('kbg_web.status_helper.resolve_book_paths')
    def test_calculate_progress_audiobook_false(self, mock_resolve):
        # Setup mock paths
        book_dir = os.path.join(self.temp_dir, "books", "testbook")
        os.makedirs(book_dir, exist_ok=True)
        config_path = os.path.join(book_dir, "config.json")
        translated_dir = os.path.join(book_dir, "translated")
        os.makedirs(translated_dir, exist_ok=True)
        cache_dir = os.path.join(book_dir, "cache")
        os.makedirs(cache_dir, exist_ok=True)
        batches_dir = os.path.join(book_dir, "batches")
        os.makedirs(batches_dir, exist_ok=True)
        audio_dir = os.path.join(book_dir, "audio")
        os.makedirs(audio_dir, exist_ok=True)
        
        mock_resolve.return_value = {
            "book_dir": book_dir,
            "config_path": config_path,
            "cache_dir": cache_dir,
            "translate_cache": os.path.join(cache_dir, "translate_cache.json"),
            "batches_dir": batches_dir,
            "translated_dir": translated_dir,
            "audio_dir": audio_dir,
            "target_lang": "uk",
            "source_lang": "en"
        }
        
        # Write config.json with generate_audiobook = False
        config_data = {
            "is_manga": False,
            "generate_audiobook": False,
            "target_lang": "uk",
            "source_lang": "en"
        }
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config_data, f)

        merged_translated = os.path.join(translated_dir, "merged_translated_uk.md")
        with open(merged_translated, "w") as f:
            f.write("Some text")

        # Call calculate_progress
        res = calculate_progress("testbook")
        self.assertEqual(res["marker_percent"], 100.0)
        self.assertEqual(res["translation_percent"], 100.0)
        # Should be 100% since audio/stress are disabled and ignored
        self.assertEqual(res["overall_percent"], 100.0)

    @patch('kbg_web.status_helper.resolve_book_paths')
    def test_calculate_progress_manga(self, mock_resolve):
        # Setup mock paths
        book_dir = os.path.join(self.temp_dir, "books", "testmanga")
        os.makedirs(book_dir, exist_ok=True)
        config_path = os.path.join(book_dir, "config.json")
        
        mock_resolve.return_value = {
            "book_dir": book_dir,
            "config_path": config_path,
            "cache_dir": os.path.join(book_dir, "cache"),
            "translate_cache": os.path.join(book_dir, "cache", "translate_cache.json"),
            "batches_dir": os.path.join(book_dir, "batches"),
            "translated_dir": os.path.join(book_dir, "translated"),
            "audio_dir": os.path.join(book_dir, "audio"),
            "target_lang": "uk",
            "source_lang": "ja"
        }
        
        # Write config.json
        config_data = {
            "is_manga": True,
            "generate_audiobook": False,
            "target_lang": "uk",
            "source_lang": "ja"
        }
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config_data, f)

        # Write manga_progress.json
        manga_progress_path = os.path.join(book_dir, "manga_progress.json")
        with open(manga_progress_path, "w", encoding="utf-8") as f:
            json.dump({"current_page": 5, "total_pages": 10}, f)

        # Call calculate_progress
        res = calculate_progress("testmanga")
        self.assertTrue(res["is_manga"])
        self.assertEqual(res["manga_percent"], 50.0)
        self.assertEqual(res["overall_percent"], 50.0)

if __name__ == '__main__':
    unittest.main()
