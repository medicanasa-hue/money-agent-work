import unittest
from unittest.mock import patch

from app.services import llm


class TestScriptFormatting(unittest.TestCase):
    def test_separate_annotations_preserve_intervening_narration(self):
        examples = (
            (
                "Başla [kamera] önemli bilgi [müzik] bitir.",
                "Başla  önemli bilgi  bitir.",
            ),
            (
                "Başla (kamera) önemli bilgi (müzik) bitir.",
                "Başla  önemli bilgi  bitir.",
            ),
            ("[ilk]Birinci.\n\n[ikinci]İkinci.", "Birinci.\n\nİkinci."),
        )
        for response, expected in examples:
            with (
                self.subTest(response=response),
                patch.object(llm, "_generate_response", return_value=response),
            ):
                self.assertEqual(llm.generate_script("Türkçe anlatım"), expected)
