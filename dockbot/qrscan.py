"""Чтение QR пропуска из фотографии. Снимок в файл не пишется."""

from __future__ import annotations

import numpy as np
import cv2


def decode_qr(image: bytes) -> str | None:
    """Текст QR или None, если кадра нет или код не читается."""
    if not image:
        return None
    try:
        array = np.frombuffer(image, dtype=np.uint8)
        picture = cv2.imdecode(array, cv2.IMREAD_COLOR)
        if picture is None:
            return None
        value, _points, _straight = cv2.QRCodeDetector().detectAndDecode(picture)
    except Exception:
        return None
    text = (value or "").strip()
    return text or None
