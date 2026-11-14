"""Локальное чтение карточки: ФИО и госномер."""

from __future__ import annotations

import io
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageFont

from dockbot.documents import judge, ocr_gap, read_document

PLATE = "А123ВС178"
NAME = "Иванов Иван Иванович"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def _card(*lines: str) -> bytes:
    font = ImageFont.truetype(FONT, 72)
    image = Image.new("RGB", (1500, 460), "white")
    draw = ImageDraw.Draw(image)
    y = 70
    for line in lines:
        draw.text((48, y), line, fill="black", font=font)
        y += 140
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


class DocumentReadTests(unittest.TestCase):
    def test_clear_card_reads_name_and_plate(self) -> None:
        image = _card("ИВАНОВ ИВАН ИВАНОВИЧ", "ГОСНОМЕР А123ВС178")
        reading = read_document(image, PLATE, NAME)
        self.assertTrue(reading.confident, reading.text)
        self.assertEqual(reading.verdict, "match")
        self.assertIsNone(reading.gap)
        folded = reading.text.upper()
        self.assertIn("ИВАНОВ", folded)
        self.assertIn("ИВАН", folded)
        self.assertIn("ИВАНОВИЧ", folded)

    def test_latin_plate_letters_match_the_cyrillic_number(self) -> None:
        reading = judge(
            "ИВАНОВ ИВАН ИВАНОВИЧ\nГОСНОМЕР A123BC178",
            PLATE,
            NAME,
        )
        self.assertTrue(reading.confident)
        self.assertEqual(reading.verdict, "match")

    def test_gap_names_the_missing_piece(self) -> None:
        self.assertIn("rus.traineddata", ocr_gap(False, None) or "")
        self.assertIn("rus.traineddata", ocr_gap(True, {"eng", "osd"}) or "")
        self.assertIsNone(ocr_gap(True, {"eng", "osd", "rus"}))

    def test_missing_tesseract_is_logged_and_not_called_a_bad_photo(self) -> None:
        image = _card("ИВАНОВ ИВАН ИВАНОВИЧ", "ГОСНОМЕР А123ВС178")
        gap = ocr_gap(False, None)
        with patch("dockbot.documents._probe", return_value=(None, gap, set())):
            with self.assertLogs("dockbot.documents", level="ERROR") as caught:
                reading = read_document(image, PLATE, NAME)
        self.assertFalse(reading.confident)
        self.assertEqual(reading.gap, gap)
        self.assertIn("Tesseract executable was not found", "\n".join(caught.output))
        self.assertIn("rus.traineddata", "\n".join(caught.output))
