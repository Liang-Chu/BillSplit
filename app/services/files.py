"""Upload validation, HEIC conversion, thumbnails."""

import hashlib
import io
from dataclasses import dataclass

from PIL import Image, ImageOps

from app.errors import APIError

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover
    pillow_heif = None

THUMB_SIZE = 400
EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "application/pdf": "pdf"}
_HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1"}


def sniff(data: bytes) -> str | None:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:5] == b"%PDF-":
        return "application/pdf"
    if data[4:8] == b"ftyp" and data[8:12] in _HEIF_BRANDS:
        return "image/heic"
    return None


@dataclass
class ProcessedFile:
    data: bytes
    mime: str
    ext: str
    thumb: bytes | None
    sha256: str


def to_pil(data: bytes, mime: str) -> Image.Image:
    """First page/frame as an upright RGB PIL image."""
    if mime == "application/pdf":
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(data)
        try:
            page = pdf[0]
            w, h = page.get_size()
            scale = min(200 / 72, 2200 / max(w, h))  # ~200 DPI, capped
            return page.render(scale=scale).to_pil().convert("RGB")
        finally:
            pdf.close()
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img)
    return img.convert("RGB")


def make_thumb(img: Image.Image) -> bytes:
    t = img.copy()
    t.thumbnail((THUMB_SIZE, THUMB_SIZE))
    buf = io.BytesIO()
    t.save(buf, "JPEG", quality=80, optimize=True)
    return buf.getvalue()


def process_upload(data: bytes, max_bytes: int) -> ProcessedFile:
    if len(data) > max_bytes:
        raise APIError(413, "file_too_large", f"File exceeds {max_bytes // (1024 * 1024)} MB")
    if not data:
        raise APIError(415, "unsupported_file_type", "Empty file")
    mime = sniff(data)
    if mime is None:
        raise APIError(415, "unsupported_file_type", "Only JPEG, PNG, WebP, HEIC and PDF files are allowed")
    try:
        if mime == "image/heic":
            if pillow_heif is None:
                raise APIError(415, "unsupported_file_type", "HEIC support is not installed")
            img = to_pil(data, mime)
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=90)
            data, mime = buf.getvalue(), "image/jpeg"
        else:
            img = to_pil(data, mime)
        thumb = make_thumb(img)
    except APIError:
        raise
    except Exception:
        raise APIError(415, "unsupported_file_type", "The file could not be read as an image or PDF")
    return ProcessedFile(data=data, mime=mime, ext=EXT[mime], thumb=thumb, sha256=hashlib.sha256(data).hexdigest())
