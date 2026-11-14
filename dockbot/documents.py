"""Сверка фото документа с госномером и ФИО.

Текст читает локальный Tesseract. На Windows движок с русским и
английским лежит в репозитории и при старте копируется в .venv/tesseract.
Если копии нет, та же сборка скачивается. Сбой пишется в журнал отдельной
строкой.
Платного API и новых секретов нет. Неуверенный кадр не подменяет сверку:
фото остаётся, результат подтверждает водитель.
Если программу так и не удалось подготовить, причина пишется в журнал.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import unicodedata
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import cv2
import numpy as np
from PIL import Image

from dockbot.config import PROJECT_ROOT

log = logging.getLogger(__name__)

MATCH = "match"
MISMATCH = "mismatch"

# Буквы номера и их двойники: латиница и греческие той же формы.
# С/C, М/M и остальные не должны делать из той же строки чужое ФИО.
# Лунную сигму переводим до NFKC: нормализация превращает её в обычную Σ.
_PLATE_LETTERS = "АВЕКМНОРСТУХ"
_LATIN_TWINS = "ABEKMHOPCTYX"
_GREEK_TWINS = "\u0391\u0392\u0395\u0397\u039a\u039c\u039d\u039f\u03a1\u03a4\u03a5\u03a7\u03f9"
_GREEK_TWINS_LOWER = "\u03b1\u03b2\u03b5\u03b7\u03ba\u03bc\u03bd\u03bf\u03c1\u03c4\u03c5\u03c7\u03f2"
_TWIN_INDEX = (0, 1, 2, 5, 3, 4, 5, 6, 7, 9, 10, 11, 8)
_HOMOGLYPHS = str.maketrans(
    {
        **{ord(src): _PLATE_LETTERS[i] for i, src in enumerate(_LATIN_TWINS)},
        **{ord(src.lower()): _PLATE_LETTERS[i] for i, src in enumerate(_LATIN_TWINS)},
        **{
            ord(src): _PLATE_LETTERS[i]
            for src, i in zip(_GREEK_TWINS + _GREEK_TWINS_LOWER, _TWIN_INDEX * 2)
        },
    }
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
# Portable Tesseract 5.5.3 and tessdata_fast 4.1.0 (the fast rus/eng models
# Debian ships). Digests are the GitHub release asset checksums.
_ENGINE_RELEASE = (
    "https://github.com/mikhailgarber/tesseract-portable/releases/download/v5.5.3_5"
)
_LANG_RELEASE = (
    "https://github.com/mikhailgarber/tesseract-portable/releases/download/"
    "tessdata_fast-4.1.0"
)
_DOWNLOAD_LIMIT = 20_000_000


@dataclass(frozen=True)
class _Asset:
    url: str
    sha256: str


@dataclass(frozen=True)
class _EngineSpec:
    asset: _Asset
    binary_name: str


@dataclass(frozen=True)
class LocalEngine:
    binary: Path
    tessdata: Path


_LANG_FILES = {
    "eng.traineddata": _Asset(
        f"{_LANG_RELEASE}/eng.traineddata",
        "7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2",
    ),
    "rus.traineddata": _Asset(
        f"{_LANG_RELEASE}/rus.traineddata",
        "e16e5e036cce1d9ec2b00063cf8b54472625b9e14d893a169e2b0dedeb4df225",
    ),
}
_ENGINE_SPECS = {
    "windows": _EngineSpec(
        _Asset(
            f"{_ENGINE_RELEASE}/tesseract-5.5.3_5-x64-windows-static.tar.gz",
            "be50ebf6b9cb5c58b440e3531fd4b5ca4ff476cf9505305b776ed6691fc9e3c1",
        ),
        "tesseract.exe",
    ),
    "linux": _EngineSpec(
        _Asset(
            f"{_ENGINE_RELEASE}/tesseract-5.5.3_5-x64-linux-portable.tar.gz",
            "52c198f697eb8c9e49f20597f319bf384f15b5f8361dc1aa1b8b3254e04b610c",
        ),
        "tesseract",
    ),
}
ENGINE_ROOT = PROJECT_ROOT / ".venv" / "tesseract"
_BUNDLE = Path(__file__).resolve().parent / "vendor" / "tesseract"
_EXE_SHA256 = {
    "tesseract.exe": "5f26f0ce42679b2baebdf636c27353457590b799a96eed79f5a7859862a6aa76",
}


@dataclass(frozen=True)
class OcrEngine:
    binary: Path
    tessdata: Path | None = None
_probe_cache: tuple[object | None, str | None, set[str]] | None = None
_probe_lock = threading.Lock()


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
            "Tesseract executable was not found. "
            "Expected a local engine in .venv/tesseract "
            "with rus.traineddata and eng.traineddata."
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
    """Сходится, только если на фото есть этот номер и это ФИО.

    Регистр, ё/е и латинские двойники кириллицы не различают ни ФИО, ни номер.
    Другой распознанный госномер или другое ФИО — расхождение.
    Обрывок текста остаётся неуверенным.
    """
    folded = _fold(text)
    expected = _fold(plate)
    plate_hit = _plate_present(folded, expected)
    name_hit = _name_present(text, driver_name)
    others = [item for item in _PLATE.findall(folded) if item != expected]
    if plate_hit and name_hit and not others:
        return Reading(text, True, MATCH)
    if _different_name(text, driver_name):
        return Reading(text, True, MISMATCH)
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
    picture = Image.open(io.BytesIO(image)).convert("RGB")
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


def prepare_ocr() -> None:
    """Найти rus+eng до опроса Telegram. Сбой чтения не останавливает бота."""
    log.disabled = False
    try:
        engine, gap, languages = _probe()
    except Exception as exc:
        log.error("document OCR is unavailable: %s", _short(exc))
        return
    if engine is None:
        log.error("document OCR is unavailable: %s", gap)
        return
    ready = ", ".join(name for name in ("rus", "eng") if name in languages)
    log.info("document OCR ready (%s)", ready)


def ensure_local_engine(root: Path | None = None) -> LocalEngine:
    """Положить переносной Tesseract и rus+eng в .venv.

    Сначала копируется сборка из репозитория. Скачивание — только если
    её нет. Каталог создаётся до сети, ошибка не проглатывается.
    """
    directory = root or ENGINE_ROOT
    ready = _ready_local(directory)
    if ready is not None:
        return ready
    directory.mkdir(parents=True, exist_ok=True)
    bundled = _install_bundle(directory)
    if bundled is not None:
        return bundled
    spec = _engine_spec()
    if spec is None:
        raise RuntimeError(
            f"no portable Tesseract build for {platform.system()} {platform.machine()}"
        )
    log.info("document OCR: fetching engine into %s", directory)
    _extract_engine(_download(spec.asset), directory, spec.binary_name)
    tessdata = directory / "tessdata"
    for name, asset in _LANG_FILES.items():
        _write_bytes(tessdata / name, _download(asset))
    ready = _ready_local(directory)
    if ready is None:
        raise RuntimeError(
            "local Tesseract files did not match the expected checksums"
        )
    return ready


def _probe() -> tuple[OcrEngine | None, str | None, set[str]]:
    global _probe_cache
    # Alembic fileConfig disables loggers created before the migration.
    log.disabled = False
    with _probe_lock:
        if _probe_cache is None:
            _probe_cache = _locate()
        return _probe_cache


def _locate() -> tuple[OcrEngine | None, str | None, set[str]]:
    local = _ready_local(ENGINE_ROOT)
    if local is not None:
        bound = _activate(local.binary, local.tessdata)
        if bound[0] is not None:
            return bound
    system = _system_reading()
    if system is not None:
        return system
    try:
        local = ensure_local_engine(ENGINE_ROOT)
    except Exception as exc:
        reason = _short(exc)
        log.error("document OCR fetch failed: %s", reason)
        gap = f"{ocr_gap(False, None)} Automatic fetch failed: {reason}"
        return (None, gap, set())
    return _activate(local.binary, local.tessdata)


def _activate(
    binary: Path, tessdata: Path | None
) -> tuple[OcrEngine | None, str | None, set[str]]:
    languages, error = _list_langs(binary, tessdata)
    if error:
        log.error("document OCR could not start: %s", error)
        gap = f"{ocr_gap(False, None)} {error}"
        return (None, gap, languages)
    gap = ocr_gap(True, languages)
    if gap:
        return (None, gap, languages)
    return (OcrEngine(binary, tessdata), None, languages)


def _system_reading() -> tuple[OcrEngine, None, set[str]] | None:
    binary = _system_binary()
    if binary is None:
        return None
    languages, error = _list_langs(binary, None)
    if error or ocr_gap(True, languages) is not None:
        return None
    return (OcrEngine(binary, None), None, languages)


def _system_binary() -> Path | None:
    found = shutil.which("tesseract")
    if found and os.path.isfile(found):
        return Path(found)
    for candidate in _WINDOWS_TESSERACT:
        if os.path.isfile(candidate):
            return Path(candidate)
    return None


def _list_langs(
    binary: Path, tessdata: Path | None
) -> tuple[set[str], str | None]:
    command = [str(binary), "--list-langs"]
    if tessdata is not None:
        command.extend(["--tessdata-dir", str(tessdata)])
    try:
        proc = _spawn(command)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return set(), _short(exc)
    if proc.returncode not in (0, 1):
        detail = _stderr_line(proc.stderr) or f"exit {proc.returncode}"
        return set(), detail
    languages = {
        line.strip()
        for line in proc.stdout.decode("utf-8", "replace").splitlines()
        if re.fullmatch(r"[a-z_]+", line.strip())
    }
    return languages, None


def _install_bundle(directory: Path) -> LocalEngine | None:
    """Скопировать Windows-сборку из репозитория. None — её здесь нет."""
    spec = _engine_spec()
    if spec is None or spec.binary_name != "tesseract.exe":
        return None
    source_exe = _BUNDLE / "tesseract.exe"
    if not source_exe.is_file():
        return None
    log.info("document OCR: installing bundled engine into %s", directory)
    _write_bytes(directory / "tesseract.exe", source_exe.read_bytes())
    (directory / "tesseract.exe").chmod(0o755)
    for name in _LANG_FILES:
        source = _BUNDLE / "tessdata" / name
        if not source.is_file():
            log.error("document OCR: bundled %s is missing", name)
            return None
        _write_bytes(directory / "tessdata" / name, source.read_bytes())
    licenses = _BUNDLE / "LICENSES"
    if licenses.is_dir():
        for item in licenses.iterdir():
            if item.is_file():
                _write_bytes(directory / "LICENSES" / item.name, item.read_bytes())
    ready = _ready_local(directory)
    if ready is None:
        log.error("document OCR: bundled engine checksum did not match")
        return None
    return ready


def _engine_spec() -> _EngineSpec | None:
    system = platform.system().lower()
    machine = platform.machine().lower()
    # 64-bit Windows reports AMD64. The bundled executable is x64.
    if system == "windows":
        if sys.maxsize <= 2**32 or machine not in {"amd64", "x86_64", "x64"}:
            return None
        return _ENGINE_SPECS.get("windows")
    if machine not in {"amd64", "x86_64"}:
        return None
    return _ENGINE_SPECS.get(system)


def _ready_local(root: Path) -> LocalEngine | None:
    spec = _engine_spec()
    if spec is None:
        return None
    binary = root / spec.binary_name
    tessdata = root / "tessdata"
    if not binary.is_file():
        return None
    expected_exe = _EXE_SHA256.get(spec.binary_name)
    if expected_exe is not None and _sha256(binary) != expected_exe:
        return None
    for name, asset in _LANG_FILES.items():
        path = tessdata / name
        if not path.is_file() or _sha256(path) != asset.sha256:
            return None
    return LocalEngine(binary, tessdata)


def _download(asset: _Asset) -> bytes:
    request = urllib.request.Request(
        asset.url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; DockBot/1.0)"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = _read_limited(response, _DOWNLOAD_LIMIT)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"download failed (HTTP {exc.code})") from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None)
        name = reason.__class__.__name__ if reason is not None else "URLError"
        raise RuntimeError(f"download failed ({name})") from exc
    except (TimeoutError, OSError) as exc:
        raise RuntimeError(f"download failed ({exc.__class__.__name__})") from exc
    if hashlib.sha256(payload).hexdigest() != asset.sha256:
        raise RuntimeError("download checksum did not match")
    return payload


def _read_limited(response, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(1 << 16)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise RuntimeError("download is larger than expected")
        chunks.append(chunk)
    return b"".join(chunks)


def _extract_engine(payload: bytes, root: Path, binary_name: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    found = False
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        for member in archive.getmembers():
            if not member.isfile() or member.size > _DOWNLOAD_LIMIT:
                continue
            pure = PurePosixPath(member.name.replace("\\", "/"))
            if pure.is_absolute() or ".." in pure.parts:
                continue
            extracted = archive.extractfile(member)
            if extracted is None:
                continue
            if pure.as_posix() == f"bin/{binary_name}":
                target = root / binary_name
                _write_bytes(target, extracted.read())
                target.chmod(0o755)
                found = True
                continue
            if pure.parts and pure.parts[0] == "LICENSES":
                target = root.joinpath(*pure.parts)
                if _inside(root, target):
                    _write_bytes(target, extracted.read())
    if not found:
        raise RuntimeError(f"archive has no {binary_name}")


def _write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    temporary.write_bytes(payload)
    temporary.replace(path)


def _inside(root: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_text(
    engine: OcrEngine, image: bytes, plate: str, driver_name: str, languages: set[str]
) -> str:
    if "rus" in languages and "eng" in languages:
        lang = "rus+eng"
    elif "rus" in languages:
        lang = "rus"
    else:
        lang = "eng"
    best = ""
    best_rank = (-1, -1)
    # Сначала блок текста справа от портрета. Штрихи лица иначе
    # прилипают к ФИО, и та же фамилия читается как чужая.
    passes: list[tuple[bytes, bool]] = [(image, False)]
    column = _text_column_png(image)
    if column is not None:
        passes.insert(0, (column, True))
    for picture, column_only in passes:
        for view in _views(picture):
            for psm in (6, 4):
                try:
                    text = _ocr_image(engine, view, lang, psm)
                except (OSError, subprocess.TimeoutExpired) as exc:
                    log.error("document OCR pass failed: %s", _short(exc))
                    continue
                rank = _rank(text, plate, driver_name)
                if rank[0] >= 3:
                    return text
                # Обрезок без ФИО не должен перебивать полный кадр.
                if column_only:
                    continue
                if rank > best_rank:
                    best, best_rank = text, rank
    return best


def _ocr_image(engine: OcrEngine, image: Image.Image, lang: str, psm: int) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        source = folder / "page.png"
        image.save(source, format="PNG")
        output = folder / "out"
        command = [
            str(engine.binary),
            str(source),
            str(output),
            "-l",
            lang,
            "--oem",
            "1",
            "--psm",
            str(psm),
        ]
        if engine.tessdata is not None:
            command.extend(["--tessdata-dir", str(engine.tessdata)])
        proc = _spawn(command)
        text_path = Path(str(output) + ".txt")
        if text_path.is_file():
            return text_path.read_text(encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            detail = _stderr_line(proc.stderr) or f"exit {proc.returncode}"
            log.error("document OCR pass failed: %s", detail)
        return ""


def _spawn(command: list[str]) -> subprocess.CompletedProcess[bytes]:
    kwargs: dict[str, object] = {
        "capture_output": True,
        "timeout": 60,
        "check": False,
        "stdin": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(command, **kwargs)


def _stderr_line(payload: bytes | None) -> str:
    if not payload:
        return ""
    line = payload.decode("utf-8", "replace").splitlines()
    return line[0][:200] if line else ""


def _short(exc: BaseException) -> str:
    text = str(exc).splitlines()[0].strip() if str(exc).strip() else ""
    return (text or exc.__class__.__name__)[:200]


def _text_column_png(image: bytes) -> bytes | None:
    """Правая колонка, если слева рисунок и его отделяет пустой промежуток."""
    array = cv2.imdecode(np.frombuffer(image, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if array is None or array.size == 0:
        return None
    height, width = array.shape[:2]
    if width < 400 or height < 80:
        return None
    ink = array < 170
    density = ink.mean(axis=0)
    limit = int(width * 0.55)
    cursor = int(width * 0.12)
    gap: tuple[int, int, int] | None = None
    while cursor < limit:
        if density[cursor] < 0.012:
            begin = cursor
            while cursor < limit and density[cursor] < 0.012:
                cursor += 1
            if cursor - begin >= 12 and (gap is None or cursor - begin > gap[0]):
                gap = (cursor - begin, begin, cursor)
        else:
            cursor += 1
    if gap is None:
        return None
    _length, begin, end = gap
    if float(ink[:, :begin].mean()) < 0.01 or float(ink[:, end:].mean()) < 0.01:
        return None
    ok, encoded = cv2.imencode(".png", array[:, end:])
    if not ok:
        return None
    return encoded.tobytes()


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


# Слова бланка, а не фамилия. «Складской талон» не должен стать чужим ФИО.
_NAME_STOP = {
    "госномер",
    "номер",
    "водитель",
    "документ",
    "путевой",
    "лист",
    "талон",
    "складской",
    "склад",
    "накладная",
    "прицеп",
    "автомобиль",
    "транспорт",
    "машина",
    "машин",
    "серия",
    "выдан",
    "россия",
    "фамилия",
    "отчество",
}
_PERSON_SUFFIXES = (
    "ович",
    "евич",
    "овна",
    "евна",
    "ична",
    "ова",
    "ева",
    "ина",
    "ов",
    "ев",
    "ин",
    "ич",
)


def _different_name(text: str, driver_name: str) -> bool:
    """На фото читается другое ФИО, не анкета."""
    if _name_present(text, driver_name):
        return False
    expected = {_soft(part) for part in driver_name.split() if len(part) >= 3}
    for token in _name_tokens(text):
        if token in expected:
            continue
        if any(
            token.startswith(part) or part.startswith(token) for part in expected
        ):
            continue
        if _looks_like_person(token):
            return True
    return False


def _name_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for token in re.findall(r"[0-9A-ZА-Я]+", _letters(text)):
        soft = token.casefold()
        if len(soft) < 3 or any(char.isdigit() for char in soft):
            continue
        if re.fullmatch(r"[а-яё]+", soft) is None or soft in _NAME_STOP:
            continue
        tokens.append(soft)
    return tokens


def _looks_like_person(token: str) -> bool:
    if len(token) < 5:
        return False
    for suffix in _PERSON_SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            return True
    return False


def _name_present(text: str, driver_name: str) -> bool:
    parts = [_soft(part) for part in driver_name.split() if len(part) >= 3]
    if not parts:
        return False
    tokens = [token.casefold() for token in re.findall(r"[0-9A-ZА-Я]+", _letters(text))]
    if all(part in tokens for part in parts):
        return True
    folded = _soft(text)
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


def _letters(text: str) -> str:
    """Верхний регистр, ё как е, двойники кириллицы. Пробелы остаются.

    Двойники снимаются до NFKC: иначе лунная сигма становится обычной Σ.
    """
    shaped = text.translate(_HOMOGLYPHS)
    shaped = unicodedata.normalize("NFKC", shaped).translate(_HOMOGLYPHS)
    shaped = shaped.upper().replace("Ё", "Е")
    return shaped.translate(_HOMOGLYPHS)


def _fold(text: str) -> str:
    return "".join(_letters(text).split())


def _soft(text: str) -> str:
    return _fold(text).casefold()
