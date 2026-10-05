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
