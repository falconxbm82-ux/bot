"""Сверка фото документа с госномером и ФИО.

Текст читает локальный Tesseract, если он есть в системе.
Платного API и новых секретов нет. Неуверенный кадр не подменяет сверку:
фото остаётся, результат подтверждает водитель.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps

from dockbot.config import PROJECT_ROOT

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


def judge(text: str, plate: str, driver_name: str) -> Reading:
    """Сходится, только если на фото есть этот номер и ФИО.

    Другой распознанный госномер — расхождение.
    Обрывок текста остаётся неуверенным.
    """
    folded = _fold(text)
    expected = _fold(plate)
    plate_hit = len(expected) >= 8 and expected in folded
    names = [_soft(part) for part in driver_name.split() if len(part) >= 3]
    tokens = [_soft(_fold(part)) for part in re.findall(r"[0-9A-Za-zА-Яа-яЁё]+", text)]
    name_hit = bool(names) and all(part in tokens for part in names)
    others = [item for item in _PLATE.findall(folded) if item != expected]
    if plate_hit and name_hit and not others:
        return Reading(text, True, MATCH)
    if others and not plate_hit:
        return Reading(text, True, MISMATCH)
    return Reading(text, False, None)


def read_document(image: bytes, plate: str, driver_name: str) -> Reading:
    prepared = prepare_image(image)
    return judge(_ocr(prepared), plate, driver_name)


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


def _ocr(image: bytes) -> str:
    try:
        import pytesseract
    except ImportError:
        return ""
    try:
        picture = Image.open(BytesIO(image))
    except Exception:
        return ""
    picture = ImageOps.grayscale(picture)
    if picture.width < 900:
        picture = picture.resize((picture.width * 2, picture.height * 2))
    for lang in ("rus+eng", "rus", "eng"):
        try:
            return pytesseract.image_to_string(picture, lang=lang)
        except Exception:
            continue
    return ""


def _fold(text: str) -> str:
    return "".join(text.upper().split()).translate(_LATIN)


def _soft(text: str) -> str:
    return text.casefold().replace("ё", "е")
