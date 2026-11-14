"""Сверка фото документа с госномером и ФИО.

Текст читает локальный Tesseract, если он есть в системе.
Платного API и новых секретов нет. Неуверенный кадр не подменяет сверку:
фото остаётся, результат подтверждает водитель.
Если программы или русского языка нет, причина пишется в журнал.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from dockbot.config import PROJECT_ROOT

log = logging.getLogger(__name__)

MATCH = "match"
MISMATCH = "mismatch"

_LATIN = str.maketrans(
    "ABEKMHOPCTYX",
    "АВЕКМНОРСТУХ",
)
_PLATE = re.compile(r"[АВЕКМНОРСТУХ]\d{3}[АВЕКМНОРСТУХ]{2}\d{2,3}")
_NAME = re.compile(r"^[0-9a-f]{32}\.jpg$")
_DOCUMENTS = PROJECT_ROOT / "data" / "documents"


class DocumentError(Exception):
    """Текст можно показать водителю."""


@dataclass(frozen=True)
class Reading:
    text: str
    confident: bool
    verdict: str | None
    gap: str | None = None


_WINDOWS_TESSERACT = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
)
# Цифра, которую Tesseract часто ставит вместо буквы номера, и наоборот.
_PLATE_CLASS = {
    "А": "АA",
    "В": "ВB8",
    "Е": "ЕE",
    "К": "КK",
    "М": "МM",
    "Н": "НH",
    "О": "ОO0",
    "Р": "РP",
    "С": "СC",
    "Т": "ТT",
    "У": "УY",
    "Х": "ХX",
    "0": "0ОO",
    "3": "3З",
    "8": "8ВB",
}
_probe_cache: tuple[object | None, str | None, set[str]] | None = None


def prepare_image(image: bytes) -> bytes:
    """Пустой и нечитаемый кадр не сохраняется."""
    if not image or len(image) < 32:
        raise DocumentError("Фото пустое. Пришлите снимок документа.")
    array = cv2.imdecode(np.frombuffer(image, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if array is None or array.size == 0:
        raise DocumentError("Фото пустое. Пришлите снимок документа.")
    height, width = array.shape[:2]
    if height < 20 or width < 20 or float(array.std()) < 2.0:
        raise DocumentError("Фото пустое. Пришлите снимок документа.")
    return image


def ocr_gap(binary_found: bool, languages: set[str] | None) -> str | None:
    """Чего не хватает для локального чтения. None — Tesseract и rus на месте."""
    if not binary_found:
        return (
            "Tesseract executable was not found. Install Tesseract OCR, "
            "add it to PATH or use C:\\Program Files\\Tesseract-OCR, "
            "and install rus.traineddata."
        )
    if not languages or "rus" not in languages:
        found = ", ".join(sorted(languages or ())) or "none"
        return (
            "Tesseract has no Russian language data (rus.traineddata). "
            f"Languages found: {found}."
        )
    if "eng" not in languages:
        return "Tesseract has no English language data (eng.traineddata)."
    return None


def judge(text: str, plate: str, driver_name: str) -> Reading:
    """Сходится, только если на фото есть этот номер и ФИО.

    Буквы номера сверяются и в кириллице, и в латинице (А/A).
    Другой распознанный госномер — расхождение.
    Обрывок текста остаётся неуверенным.
    """
    folded = _fold(text)
    expected = _fold(plate)
    plate_hit = _plate_present(folded, expected)
    name_hit = _name_present(text, driver_name)
    others = [item for item in _PLATE.findall(folded) if item != expected]
    if plate_hit and name_hit and not others:
        return Reading(text, True, MATCH)
    if others and not plate_hit:
        return Reading(text, True, MISMATCH)
    return Reading(text, False, None)


def read_document(image: bytes, plate: str, driver_name: str) -> Reading:
    prepared = prepare_image(image)
    engine, gap, languages = _probe()
    text = ""
    if engine is not None:
        text = _read_text(engine, prepared, plate, driver_name, languages)
    reading = judge(text, plate, driver_name)
    if reading.confident or not gap:
        return reading
    # Alembic fileConfig disables loggers created before the migration.
    # The missing engine must still be visible in the process log.
    log.disabled = False
    log.error("document OCR is unavailable: %s", gap)
    return Reading(reading.text, False, reading.verdict, gap)


def save_document(image: bytes) -> str:
    """Файл лежит в data/documents и в git не входит."""
    prepare_image(image)
    picture = Image.open(BytesIO(image)).convert("RGB")
    _DOCUMENTS.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}.jpg"
    picture.save(_DOCUMENTS / name, format="JPEG", quality=85)
    return name


def document_path(name: str | None) -> Path | None:
    if not name or _NAME.fullmatch(name) is None:
        return None
    return _DOCUMENTS / name


def discard_document(name: str | None) -> None:
    path = document_path(name)
    if path is not None and path.is_file():
        path.unlink()


def _probe() -> tuple[object | None, str | None, set[str]]:
    global _probe_cache
    if _probe_cache is not None:
        return _probe_cache
    try:
        import pytesseract
    except ImportError:
        _probe_cache = (None, ocr_gap(False, None), set())
        return _probe_cache
    found = _point_at_tesseract(pytesseract)
    languages: set[str] = set()
    if found:
        try:
            languages = set(pytesseract.get_languages(config=""))
        except Exception:
            found = False
            languages = set()
    _probe_cache = (pytesseract if found else None, ocr_gap(found, languages), languages)
    return _probe_cache


def _point_at_tesseract(pytesseract) -> bool:
    if shutil.which("tesseract"):
        return True
    for candidate in _WINDOWS_TESSERACT:
        if os.path.isfile(candidate):
            pytesseract.pytesseract.tesseract_cmd = candidate
            return True
    command = str(getattr(pytesseract.pytesseract, "tesseract_cmd", "") or "")
    return bool(command and os.path.isfile(command))


def _read_text(engine, image: bytes, plate: str, driver_name: str, languages: set[str]) -> str:
    if "rus" in languages and "eng" in languages:
        lang = "rus+eng"
    elif "rus" in languages:
        lang = "rus"
    else:
        lang = "eng"
    best = ""
    best_rank = (-1, -1)
    for view in _views(image):
        for psm in (6, 4):
            try:
                text = engine.image_to_string(
                    view, lang=lang, config=f"--oem 1 --psm {psm}"
                )
            except Exception as exc:
                log.error(
                    "document OCR pass failed: %s", exc.__class__.__name__
                )
                continue
            rank = _rank(text, plate, driver_name)
            if rank > best_rank:
                best, best_rank = text, rank
            if rank[0] >= 3:
                return text
    return best


def _views(image: bytes) -> list[Image.Image]:
    array = cv2.imdecode(np.frombuffer(image, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if array is None or array.size == 0:
        return []
    if float(array.mean()) < 110:
        array = cv2.bitwise_not(array)
    height, width = array.shape[:2]
    long_side = max(height, width)
    if long_side < 1600:
        scale = 1600 / long_side
        array = cv2.resize(
            array, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC
        )
    contrasted = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(array)
    sharp = cv2.addWeighted(
        contrasted, 1.6, cv2.GaussianBlur(contrasted, (0, 0), 1.2), -0.6, 0
    )
    _threshold, binary = cv2.threshold(
        sharp, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
    )
    return [Image.fromarray(sharp), Image.fromarray(binary)]


def _rank(text: str, plate: str, driver_name: str) -> tuple[int, int]:
    reading = judge(text, plate, driver_name)
    if reading.confident and reading.verdict == MATCH:
        return (3, len(text))
    if reading.confident and reading.verdict == MISMATCH:
        return (2, len(text))
    folded = _fold(text)
    hits = int(_plate_present(folded, _fold(plate))) + int(
        _name_present(text, driver_name)
    )
    return (hits, len(text.strip()))


def _plate_present(folded: str, expected: str) -> bool:
    if len(expected) < 8 or len(folded) < len(expected):
        return False
    if expected in folded:
        return True
    classes = [_PLATE_CLASS.get(char, char) for char in expected]
    for start in range(len(folded) - len(expected) + 1):
        if all(folded[start + index] in classes[index] for index in range(len(expected))):
            return True
    return False


def _name_present(text: str, driver_name: str) -> bool:
    parts = [_soft(part) for part in driver_name.split() if len(part) >= 3]
    if not parts:
        return False
    tokens = [
        _soft(_fold(token))
        for token in re.findall(r"[0-9A-Za-zА-Яа-яЁё]+", text)
    ]
    if all(part in tokens for part in parts):
        return True
    folded = _soft(_fold(text))
    return all(_part_occurs(folded, part, parts) for part in parts)


def _part_occurs(folded: str, part: str, parts: list[str]) -> bool:
    longer = [
        other
        for other in parts
        if len(other) > len(part) and other.startswith(part)
    ]
    start = 0
    while True:
        index = folded.find(part, start)
        if index < 0:
            return False
        if not any(folded.startswith(other, index) for other in longer):
            return True
        start = index + 1


def _fold(text: str) -> str:
    return "".join(text.upper().split()).translate(_LATIN)


def _soft(text: str) -> str:
    return text.casefold().replace("ё", "е")
