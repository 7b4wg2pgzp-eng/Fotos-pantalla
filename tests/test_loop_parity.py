"""El loop de fotos y el de saludos quedan como en el PR #8.

Los videos se suman en otro archivo. Estas páginas no se reescriben.
"""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"


class LoopParityTests(unittest.TestCase):
    def test_photo_loop_matches_pr8_presentation_and_logic(self):
        page = (ROOT / "templates" / "loop.html").read_text(encoding="utf-8")
        style = (FIXTURES / "pr8_photo_loop.css").read_text(encoding="utf-8")
        script = (FIXTURES / "pr8_photo_loop.js").read_text(encoding="utf-8")
        self.assertEqual(page.count(style), 1)
        self.assertEqual(page.count(script), 1)
        self.assertIn("const HOLD_MS = 25000;", script)
        self.assertIn("const FETCH_EVERY_MS = 8000;", script)
        self.assertIn("setInterval(showNext, HOLD_MS);", script)
        self.assertIn("const pendientes = [];", script)
        self.assertIn("background-size:contain;", style)
        self.assertIn("transition:opacity 1.5s ease;", style)
        self.assertNotIn("rebuildSlides", page)
        self.assertNotIn("box-shadow", page)

    def test_greeting_loop_matches_pr8(self):
        current = (ROOT / "templates" / "saludos.html").read_bytes()
        expected = (FIXTURES / "pr8_saludos.html").read_bytes()
        self.assertEqual(current, expected)


if __name__ == "__main__":
    unittest.main()
