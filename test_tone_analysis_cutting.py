import unittest
from unittest.mock import patch, MagicMock
import re

# Import functions to test
from common.utils import (
    clean_translation_text,
    translate_text_hy_mt2,
    translate_text,
    validate_translation_segment
)

class TestToneAnalysisCutting(unittest.TestCase):
    def test_clean_translation_text_normal(self):
        # (а) normal response with tag
        raw = "<tone_analysis>suspense</tone_analysis>\nЦе переклад."
        self.assertEqual(clean_translation_text(raw), "Це переклад.")

    def test_clean_translation_text_no_tag(self):
        # (б) response WITHOUT tag
        raw = "Це переклад."
        self.assertEqual(clean_translation_text(raw), "Це переклад.")

    def test_clean_translation_text_unclosed(self):
        # (в) unclosed tag
        raw = "<tone_analysis>suspense\nЦе переклад."
        self.assertEqual(clean_translation_text(raw), "Це переклад.")

    def test_clean_translation_text_multiple_tags(self):
        # (г) multiple tags
        raw = "<tone_analysis>suspense</tone_analysis> <tone_analysis>melancholic</tone_analysis>\nЦе переклад."
        self.assertEqual(clean_translation_text(raw), "Це переклад.")

    def test_clean_translation_text_leftovers_and_edge_cases(self):
        # Completely empty or only whitespace
        self.assertEqual(clean_translation_text(""), "")
        self.assertEqual(clean_translation_text(None), None)
        
        # Only tag
        self.assertEqual(clean_translation_text("<tone_analysis>suspense"), "")
        self.assertEqual(clean_translation_text("<tone_analysis>neutral</tone_analysis>"), "")
        
        # Tag at the end (should still remove)
        self.assertEqual(clean_translation_text("Це переклад.<tone_analysis>suspense"), "Це переклад.")

    @patch("common.utils.requests.get")
    @patch("common.utils.requests.post")
    def test_translate_text_hy_mt2_integration(self, mock_post, mock_get):
        # Mock requests.get for props to return chat_template indicating 7b model
        mock_get.return_value.json.return_value = {"chat_template": "startoftext"}
        
        # Mock requests.post response from LLM
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "content": "<tone_analysis>suspense</tone_analysis>\n__HTML_TAG_1__Це переклад.__HTML_TAG_2__"
        }
        mock_post.return_value = mock_resp

        # Call translate_text_hy_mt2
        result = translate_text_hy_mt2(
            text="__HTML_TAG_1__This is translation.__HTML_TAG_2__",
            base_url="http://localhost:8080/v1/chat/completions",
            source_lang="en",
            target_lang="uk"
        )
        
        # Verify the returned result has the tone_analysis tag removed
        self.assertEqual(result, "__HTML_TAG_1__Це переклад.__HTML_TAG_2__")

        # Verify that prompt contains the new instructions
        data_sent = mock_post.call_args[1]["json"]
        prompt_sent = data_sent["prompt"]
        self.assertIn("First, in a single <tone_analysis> tag", prompt_sent)
        self.assertIn("briefly state the emotional register of this passage", prompt_sent)
        self.assertIn("Then, after closing the tag, output ONLY the translation", prompt_sent)

    def test_validate_translation_segment_with_cleaned_text(self):
        original = "__HTML_TAG_1__This is translation.__HTML_TAG_2__"
        
        # Simulated raw response containing the tone analysis tags and placeholder structure
        raw_response = "<tone_analysis>suspense</tone_analysis>\n__HTML_TAG_1__Це переклад.__HTML_TAG_2__"
        
        # Clean response first
        cleaned = clean_translation_text(raw_response)
        
        # Verify placeholder validation passes on the cleaned text
        self.assertTrue(validate_translation_segment(original, cleaned))

if __name__ == "__main__":
    unittest.main()
