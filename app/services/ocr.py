"""Receipt OCR: RapidOCR (PP-OCR on ONNX Runtime) + heuristic field extraction.

Best effort only — results pre-fill a form the user confirms."""

import logging
import re
import threading
import time
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
_PHONE = re.compile(r"(\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4})|\b(TEL|PHONE|FAX)\b|电话|電話", re.IGNORECASE)
_URL = re.compile(r"(www\.|https?://|\.(com|net|org|ca)\b|@)", re.IGNORECASE)
# An address needs a house number or a postcode next to a street/city word; a street word alone
# ("Market Street", "永辉超市") is not enough.
_ADDRESS = re.compile(
    r"^\s*\d+[A-Za-z]?\s+\w+.*\b(ST|STREET|AVE|AVENUE|RD|ROAD|BLVD|DR|DRIVE|HWY|HIGHWAY|SUITE|STE|UNIT|LN|LANE|PKWY|WAY|CT|PL|PLAZA|CIR)\b\.?"
    r"|\b[A-Z]{2}\s+\d{5}(-\d{4})?\b"  # "Austin, TX 78701"
    r"|\b[A-Z]\d[A-Z]\s?\d[A-Z]\d\b"  # Canadian postcode
    r"|\d+\s*(号|號|弄|室|楼|樓)|(路|街|大道)\s*\d+",
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


# ---------------------------------------------------------------- merchant

MERCHANT_WINDOW = 8  # header rows considered; never scans into the item table
MERCHANT_MIN_SCORE = 0.55

# Labels that introduce an id/number/person rather than the store name. Matched only at the start of
# a line and only when followed by a number or a colon, so brands like "Member's Mark" or
# "COSTCO WHOLESALE #123" are not affected.
_LABELS = (
    r"customer|cust|member|mbr|acct|account|card|cashier|clerk|server|operator|oper|associate|host|"
    r"terminal|term|register|reg|lane|till|pos|station|wks|"
    r"invoice|inv|receipt|rcpt|order|ord|transaction|trans|tran|txn|trx|ref|reference|auth|approval|appr|"
    r"seq|sequence|ticket|tkt|table|tbl|check|chk|guest|batch|trace|store\s*(?:no|id)|st|tr|"
    r"收银员?|收銀員?|收款员|收款員|店员|店員|会员|會員|卡号|卡號|单号|單號|订单号?|訂單號?|流水号?|流水號?|"
    r"交易号?|交易號?|发票号?|發票號?|票号|票號|终端号?|終端號?|机号|機號|台号|台號|桌号|桌號|顾客|顧客|"
    r"客户|客戶|工号|工號|编号|編號"
)
_ID_LINE = re.compile(
    rf"^\s*(?:{_LABELS})(?![A-Za-z])\s*(?:#|no\.?|num(?:ber)?|id)?\s*[:：#.]?\s*[\w\-/]*\d", re.IGNORECASE
)
_LABEL_COLON = re.compile(rf"^\s*(?:{_LABELS})(?![A-Za-z])\s*(?:#|no\.?|id)?\s*[:：]", re.IGNORECASE)
_TIME_ONLY = re.compile(r"^\s*\d{1,2}[:：]\d{2}([:：]\d{2})?\s*([ap]\.?\s?m\.?)?\s*$", re.IGNORECASE)
_PAYMENT = re.compile(
    r"^\s*(visa|master\s*card|amex|american\s+express|discover|debit|credit|cash|ebt|chip|contactless|"
    r"apple\s*pay|google\s*pay|interac|支付宝|支付寶|微信支付?|银联|銀聯|现金|現金|刷卡)(?![A-Za-z])",
    re.IGNORECASE,
)
_TITLE = re.compile(
    r"^\s*(sales\s+)?(receipt|invoice|tax\s+invoice|order\s+summary|order\s+details|bill|statement|"
    r"(customer|merchant)\s+copy|duplicate(\s+copy)?|copy|reprint|thank\s+you.*|welcome|"
    r"收据|收據|小票|购物小票|購物小票|发票|發票|欢迎光临|歡迎光臨|谢谢惠顾|謝謝惠顧)\W*$",
    re.IGNORECASE,
)
# "Key: value" lines (Sales Person: PATTY, 收银员：张三). Store names practically never contain a colon.
_KEY_VALUE = re.compile(r"^\s*[^:：]{1,24}[:：]\s*\S")
# Column/section headings of online orders and invoices, anywhere in the line.
_SECTION = re.compile(
    r"order\s+(summary|details|placed|number)|payment\s+(method|info)|ship(ping)?\s+(to|address|method)|"
    r"bill(ing)?\s+(to|address)|sold\s+to|deliver(y|ed)?\s+to|items?\s+ordered|"
    r"sales\s*(person|associate|rep)|收货地址|收貨地址|付款方式|支付方式|配送方式|订单详情|訂單詳情",
    re.IGNORECASE,
)
_WELCOME_PREFIX = re.compile(r"^\s*(welcome\s+to|欢迎光临|歡迎光臨)\s*", re.IGNORECASE)
_CJK = re.compile(r"[㐀-鿿豈-﫿]")


def _reject_reason(text: str) -> str | None:
    if _ID_LINE.search(text) or _LABEL_COLON.search(text):
        return "id_or_label"
    if _TIME_ONLY.search(text):
        return "time"
    if _SECTION.search(text):
        return "section_heading"
    if _KEY_VALUE.search(text) and not _TIME_ONLY.search(text):
        return "key_value"
    if _PAYMENT.search(text):
        return "payment"
    if _TITLE.search(text):
        return "title"
    if _PHONE.search(text):
        return "phone"
    if _URL.search(text):
        return "url"
    if _ADDRESS.search(text):
        return "address"
    if extract_date_text(text):
        return "date"
    if amounts_in(text):
        return "amount"
    return None


def _semantic_quality(text: str) -> float:
    """0..1: how much the line looks like a name rather than a code or a fragment."""
    cjk = len(_CJK.findall(text))
    letters = sum(c.isascii() and c.isalpha() for c in text)
    digits = sum(c.isdigit() for c in text)
    runs = [len(r) for r in re.findall(r"[A-Za-z]+", text)]
    longest = max(runs, default=0)
    initials = bool(re.fullmatch(r"\s*([A-Za-z][-.]){2,}[A-Za-z]\.?\s*", text))  # H-E-B, A.B.C.
    if cjk >= 2:
        q = 1.0
    elif letters >= 3 and (longest >= 3 or initials):
        q = 1.0
    else:
        return 0.0  # e.g. "0 # eo"
    name_chars = letters + cjk
    q -= 0.6 * digits / max(1, name_chars + digits)
    if "#" in text and name_chars < 6:
        q -= 0.4
    if not cjk and letters and text == text.lower() and longest < 4:
        q -= 0.3  # short lowercase fragment
    if name_chars < 4 and not initials:
        q -= 0.15
    return max(0.0, min(1.0, q))


def merchant_candidates(rows: list[Row]) -> list[dict]:
    """Scored header candidates (debug evidence). The window stops at the first money row (item table)."""
    out = []
    for i, row in enumerate(rows[:MERCHANT_WINDOW]):
        raw = row.text.strip()
        if amounts_in(raw) and not extract_date_text(raw):
            break  # reached the item table / totals
        text = _WELCOME_PREFIX.sub("", raw).strip() or raw
        reason = _reject_reason(text)
        quality = 0.0 if reason else _semantic_quality(text)
        if not reason and quality == 0.0:
            reason = "fragment"
        position = max(0.5, 1.0 - 0.06 * i)
        score = round(row.score * quality * position, 3)
        if not reason and score < MERCHANT_MIN_SCORE:
            reason = "low_score"
        out.append({"text": text[:120], "row": i, "score": score, "rejected": reason})
    return out


def extract_merchant(rows: list[Row]) -> tuple[str | None, float]:
    accepted = [c for c in merchant_candidates(rows) if c["rejected"] is None]
    if not accepted:
        return None, 0.0
    best = max(accepted, key=lambda c: (c["score"], -c["row"]))
    text, score = best["text"], best["score"]
    trimmed = re.sub(r"\s*(&|\+|-|/|,|\band)\s*$", "", text, flags=re.IGNORECASE)
    if trimmed != text:  # the name continues on a line OCR merged with something else
        text, score = trimmed, score * 0.85
    return text, round(min(score, 0.95), 2)


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
        "merchant_source": "body" if merchant else None,
        "total": total,
        "currency_guess": guess_currency(rows),
        "date": d.isoformat() if d else None,
        "confidence": {"merchant": merchant_c, "total": total_c, "date": date_c},
        "raw_lines": [{"text": r.text, "confidence": round(r.score, 3)} for r in rows],
        "merchant_candidates": merchant_candidates(rows),
    }


HEADER_TARGET_WIDTH = 1280
HEADER_MAX_PIXELS = 1_600_000


def header_crop(img):
    """Top of the receipt at higher resolution than the full-page pass (long receipts get shrunk a lot)."""
    w, h = img.size
    frac = 0.18 if h / max(w, 1) >= 1.8 else 0.3
    crop = img.crop((0, 0, w, max(1, int(h * frac))))
    cw, ch = crop.size
    scale = min(HEADER_TARGET_WIDTH / cw, 2.0, (HEADER_MAX_PIXELS / (cw * ch)) ** 0.5)
    if abs(scale - 1.0) > 0.05:
        crop = crop.resize((max(1, int(cw * scale)), max(1, int(ch * scale))))
    return crop


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

    def _ocr(self, img) -> list[Segment]:
        result = self._get_engine()(np.asarray(img)[:, :, ::-1].copy())  # RGB → BGR
        if result is None or result.txts is None:
            return []
        segs = []
        for text, score, box in zip(result.txts, result.scores, result.boxes):
            xs, ys = [p[0] for p in box], [p[1] for p in box]
            segs.append(Segment(text=str(text), score=float(score), x0=min(xs), y0=min(ys), x1=max(xs), y1=max(ys)))
        return segs

    def _run(self, data: bytes, mime: str) -> dict:
        s = get_settings()
        started = time.monotonic()
        full = to_pil(data, mime)
        page = full.copy()
        page.thumbnail((MAX_SIDE, MAX_SIDE))
        fields = extract_fields(self._ocr(page))
        # One bounded retry on the header only when no credible merchant was found, and only for photos
        # that lost detail in the full-page pass (long receipts / large downscale). PDFs render cleanly.
        # Total, date and raw_lines from the first pass are kept as they are.
        w, h = full.size
        lost_detail = h / max(w, 1) >= 1.8 or max(w, h) >= 2 * MAX_SIDE
        if (
            fields["merchant"] is None
            and s.ocr_header_retry
            and mime != "application/pdf"
            and lost_detail
            and time.monotonic() - started < s.ocr_timeout_seconds / 2
        ):
            rows = group_rows(self._ocr(header_crop(full)))
            merchant, conf = extract_merchant(rows)
            fields["header_lines"] = [{"text": r.text, "confidence": round(r.score, 3)} for r in rows]
            fields["merchant_candidates"] += [{**c, "pass": "header"} for c in merchant_candidates(rows)]
            if merchant:
                fields["merchant"] = merchant
                fields["merchant_source"] = "header_retry"
                fields["confidence"]["merchant"] = round(conf * 0.9, 2)
        return fields

    def read(self, data: bytes, mime: str) -> dict:
        s = get_settings()
        if not s.ocr_enabled:
            raise APIError(503, "ocr_disabled", "OCR is disabled on this server")
        with self._pending_lock:
            if self._pending >= s.ocr_max_concurrency + s.ocr_max_queue:
                raise APIError(429, "ocr_busy", "OCR is busy; try again shortly")
            self._pending += 1
        # The slot belongs to the job, not to this caller: it is released when the job finishes or is
        # cancelled while still queued — never just because the caller stopped waiting. A timed-out job
        # that is already running keeps its slot until it really ends.
        try:
            future = self._executor.submit(self._run, data, mime)
        except Exception:
            self._release()
            log.exception("OCR submit failed")
            raise APIError(422, "ocr_failed", "Could not start OCR")
        future.add_done_callback(lambda _f: self._release())  # runs exactly once (done or cancelled)
        try:
            return future.result(timeout=s.ocr_timeout_seconds)
        except FutureTimeout:
            future.cancel()  # frees the slot now if the job hadn't started yet
            raise APIError(422, "ocr_failed", "OCR timed out")
        except APIError:
            raise
        except Exception:
            log.exception("OCR failed")
            raise APIError(422, "ocr_failed", "Could not read the receipt")

    def _release(self) -> None:
        with self._pending_lock:
            if self._pending <= 0:
                log.error("OCR slot counter would go negative; ignoring extra release")
                return
            self._pending -= 1

    @property
    def pending(self) -> int:
        with self._pending_lock:
            return self._pending


_engine: OCREngine | None = None


def get_ocr() -> OCREngine:
    global _engine
    if _engine is None:
        _engine = OCREngine()
    return _engine
