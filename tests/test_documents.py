"""Локальное чтение карточки: ФИО и госномер."""

from __future__ import annotations

import hashlib
import io
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageFont

import dockbot.documents as documents
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
    def setUp(self) -> None:
        import pytesseract

        self._command = pytesseract.pytesseract.tesseract_cmd
        self._prefix = os.environ.get("TESSDATA_PREFIX")
        documents._probe_cache = None

    def tearDown(self) -> None:
        import pytesseract

        pytesseract.pytesseract.tesseract_cmd = self._command
        documents._probe_cache = None
        if self._prefix is None:
            os.environ.pop("TESSDATA_PREFIX", None)
        else:
            os.environ["TESSDATA_PREFIX"] = self._prefix

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

    def test_different_name_on_the_same_plate_is_a_mismatch(self) -> None:
        image = _card("ИВАНОВ ИВАН ИВАНОВИЧ", "ГОСНОМЕР А123ВС178")
        reading = read_document(image, PLATE, "ыфсфыфс")
        self.assertTrue(reading.confident, reading.text)
        self.assertEqual(reading.verdict, "mismatch")
        self.assertIsNone(reading.gap)
        judged = judge(
            "ИВАНОВ ИВАН ИВАНОВИЧ\nГОСНОМЕР А123ВС178",
            PLATE,
            "ыфсфыфс",
        )
        self.assertEqual(judged.verdict, "mismatch")
        slip = judge("Складской талон", PLATE, "Иванов Иван")
        self.assertFalse(slip.confident)

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

    def test_windows_asset_is_the_static_build(self) -> None:
        with (
            patch("dockbot.documents.platform.system", return_value="Windows"),
            patch("dockbot.documents.platform.machine", return_value="AMD64"),
        ):
            spec = documents._engine_spec()
        self.assertIsNotNone(spec)
        assert spec is not None
        self.assertEqual(spec.binary_name, "tesseract.exe")
        self.assertIn("x64-windows-static.tar.gz", spec.asset.url)
        self.assertEqual(
            spec.asset.sha256,
            "be50ebf6b9cb5c58b440e3531fd4b5ca4ff476cf9505305b776ed6691fc9e3c1",
        )
        rus = documents._LANG_FILES["rus.traineddata"]
        eng = documents._LANG_FILES["eng.traineddata"]
        self.assertIn("tessdata_fast-4.1.0/rus.traineddata", rus.url)
        self.assertEqual(
            rus.sha256,
            "e16e5e036cce1d9ec2b00063cf8b54472625b9e14d893a169e2b0dedeb4df225",
        )
        self.assertEqual(
            eng.sha256,
            "7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2",
        )

    def test_windows_startup_copies_the_bundled_engine_without_network(self) -> None:
        def urlopen(request, timeout=0):
            raise AssertionError("network")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "tesseract"
            with (
                patch("dockbot.documents.platform.system", return_value="Windows"),
                patch("dockbot.documents.platform.machine", return_value="AMD64"),
                patch("dockbot.documents.urllib.request.urlopen", urlopen),
            ):
                installed = documents.ensure_local_engine(root)
                documents.ensure_local_engine(root)
            self.assertEqual(installed.binary, root / "tesseract.exe")
            self.assertTrue((root / "tessdata" / "rus.traineddata").is_file())
            self.assertTrue((root / "tessdata" / "eng.traineddata").is_file())
            self.assertEqual(
                documents._sha256(root / "tesseract.exe"),
                "5f26f0ce42679b2baebdf636c27353457590b799a96eed79f5a7859862a6aa76",
            )

    def test_local_engine_reads_the_card_without_a_system_install(self) -> None:
        script = (
            "#!/bin/sh\n"
            'if [ "$1" = "--list-langs" ]; then\n'
            "  printf '%s\\n' eng rus\n"
            "  exit 0\n"
            "fi\n"
            "printf '%s\\n' 'ИВАНОВ ИВАН ИВАНОВИЧ' 'ГОСНОМЕР А123ВС178' > \"$2.txt\"\n"
            "exit 0\n"
        ).encode()
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as packed:
            info = tarfile.TarInfo("bin/tesseract")
            info.size = len(script)
            info.mode = 0o755
            packed.addfile(info, io.BytesIO(script))
        tar_bytes = archive.getvalue()
        rus = b"rus-model"
        eng = b"eng-model"
        spec = documents._EngineSpec(
            documents._Asset(
                "https://example.test/engine.tar.gz",
                hashlib.sha256(tar_bytes).hexdigest(),
            ),
            "tesseract",
        )
        languages = {
            "eng.traineddata": documents._Asset(
                "https://example.test/eng.traineddata",
                hashlib.sha256(eng).hexdigest(),
            ),
            "rus.traineddata": documents._Asset(
                "https://example.test/rus.traineddata",
                hashlib.sha256(rus).hexdigest(),
            ),
        }
        payloads = {
            spec.asset.url: tar_bytes,
            languages["eng.traineddata"].url: eng,
            languages["rus.traineddata"].url: rus,
        }
        calls = {"count": 0}

        def urlopen(request, timeout=0):
            calls["count"] += 1
            return _Body(payloads[request.full_url])

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "tesseract"
            with (
                patch.object(documents, "ENGINE_ROOT", root),
                patch.object(
                    documents,
                    "_ENGINE_SPECS",
                    {"linux": spec, "windows": spec},
                ),
                patch.object(documents, "_LANG_FILES", languages),
                patch("dockbot.documents.shutil.which", return_value=None),
                patch("dockbot.documents.urllib.request.urlopen", urlopen),
            ):
                reading = read_document(
                    _card("ИВАНОВ ИВАН ИВАНОВИЧ", "ГОСНОМЕР А123ВС178"),
                    PLATE,
                    NAME,
                )
                fetched = calls["count"]
                documents.ensure_local_engine(root)
            self.assertTrue(reading.confident, reading.text)
            self.assertEqual(reading.verdict, "match")
            self.assertIsNone(reading.gap)
            self.assertEqual(fetched, 3)
            self.assertEqual(calls["count"], 3)

    def test_bad_download_is_logged_and_not_called_a_bad_photo(self) -> None:
        def urlopen(request, timeout=0):
            return _Body(b"not-a-tesseract-archive")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "tesseract"
            with (
                patch.object(documents, "ENGINE_ROOT", root),
                patch("dockbot.documents.shutil.which", return_value=None),
                patch("dockbot.documents.urllib.request.urlopen", urlopen),
            ):
                with self.assertLogs("dockbot.documents", level="ERROR") as caught:
                    reading = read_document(
                        _card("ИВАНОВ ИВАН ИВАНОВИЧ", "ГОСНОМЕР А123ВС178"),
                        PLATE,
                        NAME,
                    )
        self.assertFalse(reading.confident)
        self.assertIsNotNone(reading.gap)
        self.assertIn("checksum", reading.gap or "")
        self.assertIn("rus.traineddata", "\n".join(caught.output))
        self.assertIn("Tesseract executable was not found", "\n".join(caught.output))


class _Body:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = len(self._payload)
        chunk = self._payload[:size]
        self._payload = self._payload[size:]
        return chunk

    def __enter__(self) -> "_Body":
        return self

    def __exit__(self, *args: object) -> bool:
        return False
