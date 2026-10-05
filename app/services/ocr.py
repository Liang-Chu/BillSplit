"""Receipt OCR: RapidOCR (PP-OCR on ONNX Runtime) + heuristic field extraction.

Best effort only — results pre-fill a form the user confirms."""

import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from datetime import date

import numpy as np

from app.config import get_settings
from app.errors import APIError
from app.services.files import to_pil

log = logging.getLogger(__name__)

MAX_SIDE = 1280  # long edge sent to OCR; 1280 matched 1600 on real receipts with less memory and time


@dataclass
class Segment:
    text: str
    score: float
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def yc(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def h(self) -> float:
        return self.y1 - self.y0


@dataclass
class Row:
    segments: list[Segment]

    @property
    def text(self) -> str:
        return " ".join(s.text for s in sorted(self.segments, key=lambda s: s.x0))

    @property
    def score(self) -> float:
        return min(s.score for s in self.segments)

    @property
    def y(self) -> float:
        return sum(s.yc for s in self.segments) / len(self.segments)


def group_rows(segments: list[Segment]) -> list[Row]:
    """Merge boxes whose vertical centers fall on the same text line."""
    rows: list[Row] = []
    for seg in sorted(segments, key=lambda s: s.yc):
        for row in rows:
            ref = row.segments[0]
            if abs(seg.yc - row.y) <= max(min(seg.h, ref.h) * 0.5, 4):
                row.segments.append(seg)
                break
        else:
            rows.append(Row([seg]))
    return sorted(rows, key=lambda r: r.y)


# ---------------------------------------------------------------- field extraction

_MONEY = re.compile(r"(?<![\d.])(?:[$¥€£]|RMB|CAD|USD)?\s*(-?\d{1,3}(?:[,\s]\d{3})*(?:[.,]\d{2})|-?\d+[.,]\d{2})(?![\d])")
_TOTAL_KW = re.compile(
    r"(GRAND\s*TOTAL|TOTAL\s*DUE|AMOUNT\s*DUE|BALANCE\s*DUE|TOTAL\s*AMOUNT|\bTOTAL\b|\bAMOUNT\b|合\s*计|总\s*计|總\s*計|实\s*付|實\s*付|应\s*付|應\s*付|金\s*额)",
    re.IGNORECASE,
)
_TOTAL_EXCLUDE = re.compile(
    r"(SUB\s*-?\s*TOTAL|\bTAX\b|\bGST\b|\bHST\b|\bPST\b|\bVAT\b|CHANGE|\bTIP\b|\bCASH\b|SAVINGS|DISCOUNT|"
    r"ITEMS?\s*SOLD|TOTAL\s*ITEMS|小\s*计|小\s*計|找\s*零|税|稅|优惠|折扣)",
    re.IGNORECASE,
)
_PHONE = re.compile(r"(\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4})|(TEL|PHONE|电话|電話)", re.IGNORECASE)
_URL = re.compile(r"(www\.|https?://|\.com\b|@)", re.IGNORECASE)
_ADDRESS = re.compile(
    r"^\d+\s+\w+|\b(ST|STREET|AVE|AVENUE|RD|ROAD|BLVD|DR|DRIVE|HWY|SUITE|STE|UNIT|LN|PKWY|WAY)\b\.?|路|街|号|號|区|區|市",
    re.IGNORECASE,
)
_DATE_PATTERNS = [
    (re.compile(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})"), "ymd"),
    (re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"), "ymd"),
    (re.compile(r"(?<!\d)(\d{1,2})[-/.](\d{1,2})[-/.](20\d{2}|\d{2})(?!\d)"), "mdy"),
]


def parse_money(s: str) -> int | None:
    s = s.replace(" ", "")
    if re.search(r"[.,]\d{2}$", s):
        whole, frac = s[:-3], s[-2:]
        whole = re.sub(r"[.,]", "", whole)
        try:
            return int(whole or "0") * 100 + int(frac) * (1 if not whole.startswith("-") else -1)
        except ValueError:
            return None
    return None


def amounts_in(text: str) -> list[int]:
    out = []
    for m in _MONEY.finditer(text):
        v = parse_money(m.group(1))
        if v is not None and v > 0:
            out.append(v)
    return out


def extract_total(rows: list[Row]) -> tuple[int | None, float]:
    candidates: list[tuple[int, int, float]] = []  # (row index, amount, confidence)
    for i, row in enumerate(rows):
        text = row.text
        if not _TOTAL_KW.search(text) or _TOTAL_EXCLUDE.search(text):
            continue
        vals = amounts_in(text)
        if not vals and i + 1 < len(rows):
            vals = amounts_in(rows[i + 1].text)
        if vals:
            strong = bool(re.search(r"GRAND|DUE|合|总|總|实|實|应|應", text, re.IGNORECASE))
            candidates.append((i, vals[-1], min(row.score, 0.99) * (1.0 if strong else 0.9)))
    if candidates:
        strong = [c for c in candidates if c[2] >= 0.9 * 0.99]
        pick = max(strong or candidates, key=lambda c: c[0])  # bottom-most match
        return pick[1], round(pick[2], 2)
    # Fallback: largest amount in the lower half
    lower = rows[len(rows) // 2 :]
    vals = [v for r in lower if not _TOTAL_EXCLUDE.search(r.text) for v in amounts_in(r.text)]
    if vals:
        return max(vals), 0.4
    return None, 0.0


def extract_merchant(rows: list[Row]) -> tuple[str | None, float]:
    for row in rows[:5]:
        text = row.text.strip()
        letters = sum(c.isalpha() for c in text)
        if letters < 2 or row.score < 0.6:
            continue
        if _PHONE.search(text) or _URL.search(text) or _ADDRESS.search(text) or extract_date_text(text):
            continue
        if amounts_in(text):
            continue
        if re.fullmatch(r"(WELCOME|RECEIPT|INVOICE|收据|收據|发票|發票).*", text, re.IGNORECASE):
            continue
        return text[:120], round(min(row.score, 0.95), 2)
    return None, 0.0


def extract_date_text(text: str) -> date | None:
    for rx, kind in _DATE_PATTERNS:
        m = rx.search(text)
        if not m:
            continue
        a, b, c = (int(x) for x in m.groups())
        try:
            if kind == "ymd":
                return date(a, b, c)
            year = c + 2000 if c < 100 else c
            try:
                return date(year, a, b)  # MM/DD/YYYY
            except ValueError:
                return date(year, b, a)  # DD/MM/YYYY
        except ValueError:
            continue
    return None


def extract_date(rows: list[Row]) -> tuple[date | None, float]:
    for row in rows:
        d = extract_date_text(row.text)
        if d:
            return d, round(min(row.score, 0.9), 2)
    return None, 0.0


def guess_currency(rows: list[Row]) -> str | None:
    text = " ".join(r.text for r in rows)
    if re.search(r"[¥￥]|RMB|元|人民币", text):
        return "CNY"
    if re.search(r"\bCAD\b|\bGST\b|\bHST\b|\bPST\b", text):
        return "CAD"
    if re.search(r"€|\bEUR\b", text):
        return "EUR"
    if re.search(r"£|\bGBP\b", text):
        return "GBP"
    if re.search(r"\$|\bUSD\b", text):
        return "USD"
    return None


def extract_fields(segments: list[Segment]) -> dict:
    rows = group_rows(segments)
    total, total_c = extract_total(rows)
    merchant, merchant_c = extract_merchant(rows)
    d, date_c = extract_date(rows)
    return {
        "merchant": merchant,
        "total": total,
        "currency_guess": guess_currency(rows),
        "date": d.isoformat() if d else None,
        "confidence": {"merchant": merchant_c, "total": total_c, "date": date_c},
        "raw_lines": [{"text": r.text, "confidence": round(r.score, 3)} for r in rows],
    }


# ---------------------------------------------------------------- engine


class OCREngine:
    def __init__(self):
        s = get_settings()
        self._engine = None
        self._load_lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=max(1, s.ocr_max_concurrency), thread_name_prefix="ocr")
        self._pending = 0
        self._pending_lock = threading.Lock()

    def _get_engine(self):
        with self._load_lock:
            if self._engine is None:
                from rapidocr import RapidOCR

                threads = get_settings().ocr_threads
                params = None
                if threads > 0:
                    params = {
                        "EngineConfig.onnxruntime.intra_op_num_threads": threads,
                        "EngineConfig.onnxruntime.inter_op_num_threads": 1,
                    }
                self._engine = RapidOCR(params=params)
            return self._engine

    def _run(self, data: bytes, mime: str) -> list[Segment]:
        img = to_pil(data, mime)
        img.thumbnail((MAX_SIDE, MAX_SIDE))
        result = self._get_engine()(np.asarray(img)[:, :, ::-1].copy())  # RGB → BGR
        if result is None or result.txts is None:
            return []
        segs = []
        for text, score, box in zip(result.txts, result.scores, result.boxes):
            xs, ys = [p[0] for p in box], [p[1] for p in box]
            segs.append(Segment(text=str(text), score=float(score), x0=min(xs), y0=min(ys), x1=max(xs), y1=max(ys)))
        return segs

    def read(self, data: bytes, mime: str) -> dict:
        s = get_settings()
        if not s.ocr_enabled:
            raise APIError(503, "ocr_disabled", "OCR is disabled on this server")
        with self._pending_lock:
            if self._pending >= s.ocr_max_concurrency + s.ocr_max_queue:
                raise APIError(429, "ocr_busy", "OCR is busy; try again shortly")
            self._pending += 1
        try:
            future = self._executor.submit(self._run, data, mime)
            try:
                segments = future.result(timeout=s.ocr_timeout_seconds)
            except FutureTimeout:
                raise APIError(422, "ocr_failed", "OCR timed out")
            except APIError:
                raise
            except Exception:
                log.exception("OCR failed")
                raise APIError(422, "ocr_failed", "Could not read the receipt")
        finally:
            with self._pending_lock:
                self._pending -= 1
        return extract_fields(segments)


_engine: OCREngine | None = None


def get_ocr() -> OCREngine:
    global _engine
    if _engine is None:
        _engine = OCREngine()
    return _engine
