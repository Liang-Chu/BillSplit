"""Excel export: a ZIP with a formatted expenses.xlsx and an attachments/ folder (relative links work offline)."""

import io
import re
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from openpyxl import Workbook
from openpyxl.drawing.image import Image as XLImage
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet
from PIL import Image
from sqlalchemy import exists, or_, select
from sqlalchemy.orm import Session

from app.currencies import minor_units
from app.models import Attachment, Expense, ExpenseShare, ExpenseType, Group, Period, Settlement, User
from app.services.balances import compute_period_balances
from app.storage import get_storage

HEADER_FILL = PatternFill("solid", fgColor="1F4E78")
HEADER_FONT = Font(bold=True, color="FFFFFF")
ZEBRA_FILL = PatternFill("solid", fgColor="F2F6FA")
TOTAL_FILL = PatternFill("solid", fgColor="DDEBF7")
THIN = Side(style="thin", color="D0D7DE")
BORDER = Border(bottom=THIN)
WRAP = Alignment(wrap_text=True, vertical="top")
TOP = Alignment(vertical="top")
THUMB_PX = 80
ROW_HEIGHT_WITH_THUMB = 64  # points (~85 px)


@dataclass
class ExportParams:
    date_from: date
    date_to: date
    group_ids: list[str] | None
    include_settlements: bool = True


def money_format(currency: str) -> str:
    digits = minor_units(currency)
    num = "#,##0" + ("." + "0" * digits if digits else "")
    return f'"{currency} "{num};[Red]"{currency} "-{num}'


def to_major(amount: int, currency: str) -> float:
    return amount / (10 ** minor_units(currency))


def safe_name(s: str | None, limit: int = 40) -> str:
    s = re.sub(r"[^\w\-]+", "_", s or "", flags=re.UNICODE).strip("_")
    return s[:limit] or "file"


def _style_header(ws: Worksheet, headers: list[str]) -> None:
    ws.append(headers)
    for cell in ws[1]:
        cell.fill, cell.font = HEADER_FILL, HEADER_FONT
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    ws.row_dimensions[1].height = 24


def _autosize(ws: Worksheet, min_w: int = 8, max_w: int = 50, fixed: dict[int, float] | None = None) -> None:
    fixed = fixed or {}
    widths: dict[int, int] = defaultdict(int)
    for row in ws.iter_rows():
        for c in row:
            if c.value is None:
                continue
            text = str(c.value)
            longest = max((len(line) for line in text.split("\n")), default=0)
            if isinstance(c.value, float):
                longest += 6
            widths[c.column] = max(widths[c.column], longest)
    for col, w in widths.items():
        ws.column_dimensions[get_column_letter(col)].width = fixed.get(col, max(min_w, min(max_w, w + 2)))
    for col, w in fixed.items():
        ws.column_dimensions[get_column_letter(col)].width = w


def _zebra(ws: Worksheet, first_row: int = 2) -> None:
    for r in range(first_row, ws.max_row + 1):
        for c in ws[r]:
            c.border = BORDER
            if (r - first_row) % 2 == 1:
                c.fill = ZEBRA_FILL


def _thumb_image(data: bytes) -> XLImage | None:
    try:
        img = Image.open(io.BytesIO(data))
        img.thumbnail((THUMB_PX * 2, THUMB_PX))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "PNG")
        buf.seek(0)
        xl = XLImage(buf)
        xl.width, xl.height = img.size
        return xl
    except Exception:
        return None


def build_export(db: Session, user: User, params: ExportParams) -> bytes:
    storage = get_storage()
    names: dict[str, str] = {}

    def name(uid: str | None) -> str:
        if uid is None:
            return ""
        if uid not in names:
            u = db.get(User, uid)
            names[uid] = u.display_name if u else "?"
        return names[uid]

    mine = or_(
        Expense.payer_id == user.id,
        exists().where(ExpenseShare.expense_id == Expense.id, ExpenseShare.user_id == user.id),
    )
    stmt = (
        select(Expense)
        .where(Expense.deleted_at.is_(None), mine)
        .where(Expense.expense_date >= params.date_from, Expense.expense_date <= params.date_to)
        .order_by(Expense.expense_date, Expense.created_at)
    )
    if params.group_ids:
        stmt = stmt.where(Expense.group_id.in_(params.group_ids))
    expenses = db.scalars(stmt).all()

    groups: dict[str, Group] = {}
    periods: dict[str, Period] = {}
    for e in expenses:
        groups.setdefault(e.group_id, db.get(Group, e.group_id))
        periods.setdefault(e.period_id, db.get(Period, e.period_id))

    settlements: list[Settlement] = []
    if params.include_settlements:
        sstmt = (
            select(Settlement)
            .where(Settlement.deleted_at.is_(None))
            .where(or_(Settlement.from_user_id == user.id, Settlement.to_user_id == user.id))
            .where(Settlement.settled_on >= params.date_from, Settlement.settled_on <= params.date_to)
            .order_by(Settlement.settled_on)
        )
        if params.group_ids:
            sstmt = sstmt.where(Settlement.group_id.in_(params.group_ids))
        settlements = db.scalars(sstmt).all()
        for s in settlements:
            groups.setdefault(s.group_id, db.get(Group, s.group_id))
            periods.setdefault(s.period_id, db.get(Period, s.period_id))

    zbuf = io.BytesIO()
    zf = zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED)
    used_paths: set[str] = set()

    def add_file(a: Attachment, when: date, label: str | None) -> str | None:
        try:
            data = storage.get(a.storage_backend, a.storage_key)
        except Exception:
            return None
        ext = a.storage_key.rsplit(".", 1)[-1]
        path = f"attachments/{when.isoformat()}_{safe_name(label)}_{a.id[-8:]}.{ext}"
        if path not in used_paths:
            zf.writestr(path, data)
            used_paths.add(path)
        return path

    def attachments_of(owner_type: str, owner_id: str) -> list[Attachment]:
        return db.scalars(
            select(Attachment)
            .where(Attachment.owner_type == owner_type, Attachment.owner_id == owner_id, Attachment.deleted_at.is_(None))
            .order_by(Attachment.created_at)
        ).all()

    wb = Workbook()

    # ------------------------------------------------------------ Expenses sheet
    ws = wb.active
    ws.title = "Expenses"
    exp_atts = {e.id: attachments_of("expense", e.id) for e in expenses}
    max_files = max((len(v) for v in exp_atts.values()), default=0)
    headers = [
        "Date", "Group", "Period", "Type", "Title", "Merchant", "Payer", "Total", "My Share", "Split",
        "Participants", "Checked", "Notes", "Invoice",
    ] + [f"File {i + 1}" for i in range(max(1, max_files))]
    _style_header(ws, headers)
    invoice_col = headers.index("Invoice") + 1
    has_items = False
    for e in expenses:
        g = groups[e.group_id]
        p = periods[e.period_id]
        etype = db.get(ExpenseType, e.expense_type_id) if e.expense_type_id else None
        my_share = next((s.amount for s in e.shares if s.user_id == user.id), 0)
        owed = [s for s in e.shares if s.user_id != e.payer_id and s.amount > 0]
        checked_n = sum(1 for s in owed if s.checked_at)
        parts = "\n".join(
            f"{name(s.user_id)}: {to_major(s.amount, g.currency):,.{minor_units(g.currency)}f}"
            + (" ✓" if s.checked_at and s.user_id != e.payer_id else "")
            + (" (guest)" if s.is_guest else "")
            for s in e.shares
        )
        has_items = has_items or bool(e.items)
        row = [
            e.expense_date, g.name, _period_label(g, p), etype.name if etype else "", e.title, e.merchant or "",
            name(e.payer_id), to_major(e.amount, g.currency), to_major(my_share, g.currency), e.split_method,
            parts, f"{checked_n}/{len(owed)}" if owed else "—", e.notes or "", "",
        ]
        ws.append(row)
        r = ws.max_row
        ws.cell(r, 1).number_format = "yyyy-mm-dd"
        for col in (8, 9):
            ws.cell(r, col).number_format = money_format(g.currency)
        for c in ws[r]:
            c.alignment = WRAP if c.column in (5, 11, 13) else TOP
        atts = exp_atts[e.id]
        for i, a in enumerate(atts):
            path = add_file(a, e.expense_date, e.merchant or e.title)
            cell = ws.cell(r, invoice_col + 1 + i)
            if path:
                cell.value = a.original_name
                cell.hyperlink = path
                cell.style = "Hyperlink"
                cell.alignment = TOP
            else:
                cell.value = f"{a.original_name} (unavailable)"
        if atts and atts[0].thumb_key:
            try:
                img = _thumb_image(storage.get(atts[0].storage_backend, atts[0].thumb_key))
            except Exception:
                img = None
            if img:
                ws.add_image(img, f"{get_column_letter(invoice_col)}{r}")
                ws.row_dimensions[r].height = ROW_HEIGHT_WITH_THUMB
    _zebra(ws)
    ws.freeze_panes = "B2"
    if ws.max_row > 1:
        ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{ws.max_row}"
    _autosize(ws, fixed={1: 12, 5: 30, 11: 30, 13: 30, invoice_col: 24})

    # ------------------------------------------------------------ Summary sheet (first tab)
    sm = wb.create_sheet("Summary", 0)
    sm.append(["BillSplit export"])
    sm["A1"].font = Font(bold=True, size=14)
    sm.append(["Person", user.display_name])
    sm.append(["Date range", f"{params.date_from.isoformat()} to {params.date_to.isoformat()}"])
    sm.append(["Groups", ", ".join(sorted(g.name for g in groups.values())) or "(none)"])
    sm.append([])
    head_row = sm.max_row + 1
    sm.append(["Group", "Period", "Status", "Currency", "Bills", "Group spent", "I paid", "My share",
               "Net (paid − share)", "Still owed to me", "I still owe"])
    for c in sm[head_row]:
        c.fill, c.font = HEADER_FILL, HEADER_FONT
    per: dict[tuple[str, str], dict] = {}
    for e in expenses:
        k = (e.group_id, e.period_id)
        d = per.setdefault(k, {"bills": 0, "spent": 0, "paid": 0, "share": 0})
        d["bills"] += 1
        d["spent"] += e.amount
        if e.payer_id == user.id:
            d["paid"] += e.amount
        d["share"] += next((s.amount for s in e.shares if s.user_id == user.id), 0)
    for s in settlements:
        per.setdefault((s.group_id, s.period_id), {"bills": 0, "spent": 0, "paid": 0, "share": 0})
    subtotal: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0, 0, 0])
    for (gid, pid), d in sorted(per.items(), key=lambda kv: (groups[kv[0][0]].name, periods[kv[0][1]].starts_on)):
        g, p = groups[gid], periods[pid]
        b = compute_period_balances(db, p)
        owed_to_me = sum(a for _, t, a in b.pairwise if t == user.id)
        i_owe = sum(a for f, _, a in b.pairwise if f == user.id)
        vals = [d["spent"], d["paid"], d["share"], d["paid"] - d["share"], owed_to_me, i_owe]
        sm.append([g.name, _period_label(g, p), p.status, g.currency, d["bills"]] + [to_major(v, g.currency) for v in vals])
        for col in range(6, 12):
            sm.cell(sm.max_row, col).number_format = money_format(g.currency)
        for i, v in enumerate(vals):
            subtotal[g.currency][i] += v
    for cur, vals in sorted(subtotal.items()):
        sm.append([f"Total ({cur})", "", "", cur, ""] + [to_major(v, cur) for v in vals])
        for c in sm[sm.max_row]:
            c.font, c.fill = Font(bold=True), TOTAL_FILL
        for col in range(6, 12):
            sm.cell(sm.max_row, col).number_format = money_format(cur)
    _autosize(sm, max_w=40, fixed={1: 22})

    # ------------------------------------------------------------ Line items
    if has_items:
        li = wb.create_sheet("Line Items")
        _style_header(li, ["Date", "Group", "Bill", "Item", "Amount", "Shared by"])
        for e in expenses:
            g = groups[e.group_id]
            for it in e.items:
                who = ", ".join(name(p.user_id) for p in it.participants) or "Everyone on the bill"
                li.append([e.expense_date, g.name, e.title, it.name, to_major(it.amount, g.currency), who])
                li.cell(li.max_row, 1).number_format = "yyyy-mm-dd"
                li.cell(li.max_row, 5).number_format = money_format(g.currency)
        _zebra(li)
        li.freeze_panes = "A2"
        li.auto_filter.ref = f"A1:F{li.max_row}"
        _autosize(li, fixed={1: 12})

    # ------------------------------------------------------------ Settlements
    if params.include_settlements:
        st = wb.create_sheet("Payments")
        s_atts = {s.id: attachments_of("settlement", s.id) for s in settlements}
        n_files = max((len(v) for v in s_atts.values()), default=0)
        _style_header(st, ["Date", "Group", "Period", "From", "To", "Amount", "Note"]
                      + [f"File {i + 1}" for i in range(max(1, n_files))])
        for s in settlements:
            g, p = groups[s.group_id], periods[s.period_id]
            st.append([s.settled_on, g.name, _period_label(g, p), name(s.from_user_id), name(s.to_user_id),
                       to_major(s.amount, g.currency), s.note or ""])
            r = st.max_row
            st.cell(r, 1).number_format = "yyyy-mm-dd"
            st.cell(r, 6).number_format = money_format(g.currency)
            for i, a in enumerate(s_atts[s.id]):
                path = add_file(a, s.settled_on, f"payment_{name(s.from_user_id)}")
                cell = st.cell(r, 8 + i)
                cell.value = a.original_name if path else f"{a.original_name} (unavailable)"
                if path:
                    cell.hyperlink, cell.style = path, "Hyperlink"
        _zebra(st)
        st.freeze_panes = "A2"
        if st.max_row > 1:
            st.auto_filter.ref = f"A1:{get_column_letter(st.max_column)}{st.max_row}"
        _autosize(st, fixed={1: 12})

    xbuf = io.BytesIO()
    wb.save(xbuf)
    zf.writestr("expenses.xlsx", xbuf.getvalue())
    zf.close()
    return zbuf.getvalue()


def _period_label(g: Group, p: Period) -> str:
    if not g.is_recurring:
        return "—"
    if g.cadence == "monthly" and p.ends_on and p.starts_on.day == (g.anchor_day or 1) == 1:
        return p.starts_on.strftime("%Y-%m")
    end = p.ends_on.isoformat() if p.ends_on else "…"
    return f"{p.starts_on.isoformat()} – {end}"
