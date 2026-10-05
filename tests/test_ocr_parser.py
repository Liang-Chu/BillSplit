from app.services.ocr import Segment, extract_fields


def seg(text, y, x=10, w=200, score=0.98):
    return Segment(text=text, score=score, x0=x, y0=y, x1=x + w, y1=y + 30)


def test_extracts_total_merchant_date_from_split_boxes():
    segs = [
        seg("H-E-B", 10), seg("1234 Main St, Austin TX", 60), seg("(512) 555-0100", 110), seg("10/04/2026 14:22", 160),
        seg("MILK 2%", 210), seg("3.99", 212, x=400, w=60),
        seg("SUBTOTAL", 260), seg("10.77", 261, x=400, w=60),
        seg("TAX", 310), seg("0.89", 309, x=400, w=60),
        seg("TOTAL", 360), seg("11.66", 362, x=400, w=60),
        seg("CHANGE", 410), seg("0.00", 410, x=400, w=60),
    ]
    r = extract_fields(segs)
    assert r["merchant"] == "H-E-B"
    assert r["total"] == 1166
    assert r["date"] == "2026-10-04"


def test_chinese_receipt():
    segs = [seg("全家便利店", 10), seg("2026年10月3日", 60), seg("小计 ¥45.00", 110), seg("合计 ¥45.00", 160), seg("找零 ¥5.00", 210)]
    r = extract_fields(segs)
    assert r["merchant"] == "全家便利店"
    assert r["total"] == 4500
    assert r["date"] == "2026-10-03"
    assert r["currency_guess"] == "CNY"


def test_fallback_largest_amount():
    segs = [seg("Corner Shop", 10), seg("thing 2.00", 60), seg("other 3.50", 110), seg("5.50", 160)]
    r = extract_fields(segs)
    assert r["total"] == 550
    assert r["confidence"]["total"] < 0.5


# ---------------------------------------------------------------- merchant regressions (synthetic data only)

import pytest  # noqa: E402

from app.services.ocr import extract_merchant, group_rows, header_crop  # noqa: E402


def rows_of(*texts, score=0.97):
    return group_rows([seg(t, 10 + 50 * i, score=score) for i, t in enumerate(texts)])


ITEMS = ["BANANAS 1.99", "ORGANIC KALE 3.49", "SUBTOTAL 5.48", "TOTAL 221.80"]


def test_missing_store_name_gives_no_merchant_but_keeps_total_and_date():
    segs = [seg(t, 10 + 50 * i) for i, t in enumerate(["(555) 010-0199", "10/04/26 1:57 pm", "Customer # 0", *ITEMS])]
    r = extract_fields(segs)
    assert r["merchant"] is None and r["confidence"]["merchant"] == 0.0
    assert r["total"] == 22180 and r["date"] == "2026-10-04"
    assert any(c["text"] == "Customer # 0" and c["rejected"] for c in r["merchant_candidates"])


@pytest.mark.parametrize("line", [
    "0 # eo", "Customer # 0", "Invoice # 1234567", "Cashier: PATTY", "Sales Person: PATTY", "Terminal 3",
    "TERM 004 REG 2", "Trans# 88812", "1:57 pm", "13:05", "VISA", "Mastercard Debit", "Order Summary",
    "RECEIPT", "Customer Copy", "Ship to Payment method Order Summary", "收银员：张三", "单号 20261004001",
])
def test_non_merchant_lines_are_rejected(line):
    assert extract_merchant(rows_of(line, *ITEMS)) == (None, 0.0)


def test_low_confidence_fragment_is_rejected():
    assert extract_merchant(rows_of("0 # eo", score=0.602)) == (None, 0.0)


@pytest.mark.parametrize("name", [
    "Tim's Oriental & Seafood Market", "H-E-B", "7-ELEVEN", "COSTCO WHOLESALE #123", "Market Street",
    "永辉超市", "全家便利店", "TARGET T-1234",
])
def test_real_brands_pass(name):
    merchant, conf = extract_merchant(rows_of(name, "(555) 010-0199", *ITEMS))
    assert merchant == name and conf >= 0.55


def test_store_name_before_or_after_customer_number():
    name = "Tim's Oriental & Seafood Market"
    assert extract_merchant(rows_of(name, "Customer # 0", *ITEMS))[0] == name
    assert extract_merchant(rows_of("Customer # 0", name, *ITEMS))[0] == name


def test_name_after_five_header_lines_is_still_found():
    header = ["(555) 010-0199", "1234 Main St", "Austin, TX 78701", "10/04/2026", "1:57 pm", "Invoice # 1234567"]
    assert extract_merchant(rows_of(*header, "Corner Grocery", *ITEMS))[0] == "Corner Grocery"


def test_window_stops_at_item_table():
    # all header lines rejected; item names below the first priced row must not be picked
    assert extract_merchant(rows_of("Customer # 0", "BANANAS 1.99", "ORGANIC KALE", "FRESH BREAD"))[0] is None


def test_window_is_bounded():
    header = ["Customer # 0"] * 8
    assert extract_merchant(rows_of(*header, "Late Line Shop"))[0] is None


def test_dangling_connector_is_trimmed():
    merchant, conf = extract_merchant(rows_of("Tim's Oriental &", "7015 Some Rd", *ITEMS))
    assert merchant == "Tim's Oriental" and conf < 0.95


def test_welcome_prefix_is_stripped():
    assert extract_merchant(rows_of("Welcome to H-E-B", *ITEMS))[0] == "H-E-B"


def test_header_crop_is_top_region_with_bounded_pixels():
    from PIL import Image

    long_receipt = Image.new("RGB", (600, 3000), "white")
    crop = header_crop(long_receipt)
    assert crop.size[0] == 1200  # upscaled (max 2x) toward 1280
    assert crop.size[0] * crop.size[1] <= 1_600_000
    assert abs(crop.size[1] / crop.size[0] - (3000 * 0.18) / 600) < 0.02
