"""Токен, PIN и картинка QR-пропуска.

Распознавание QR остаётся на OpenCV и в этот срез не входит.
Длина PIN — стендовое значение, не норма ТЗ.
"""

from __future__ import annotations

import io
import secrets

import qrcode

PIN_LENGTH = 6


def new_token() -> str:
    return secrets.token_urlsafe(32)


def new_pin() -> str:
    limit = 10**PIN_LENGTH
    return f"{secrets.randbelow(limit):0{PIN_LENGTH}d}"


def qr_png(payload: str) -> bytes:
    code = qrcode.QRCode(border=2, box_size=8)
    code.add_data(payload)
    code.make(fit=True)
    image = code.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
