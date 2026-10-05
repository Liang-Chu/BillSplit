import io
import zipfile
from datetime import date, timedelta

from openpyxl import load_workbook

from tests.conftest import Api, login, make_type, pdf_bytes, png_bytes, register


def make_livexpense(admin: Api) -> dict:
    return admin.post(
        "/groups",
        {"name": "Livexpense", "currency": "USD", "is_recurring": True,
         "recurrence": {"cadence": "monthly", "anchor_day": 1, "timezone": "America/Chicago", "grace_days": 3}},
        expect=201,
    )


def test_bootstrap_and_seed(admin: Api):
    me = admin.get("/me")
    assert me["is_super_admin"] and me["default_group_id"]
    groups = admin.get("/groups")["items"]
    assert [g["name"] for g in groups][:1] == ["Personal"]
    assert [g["name"] for g in groups] == ["Personal"]  # only the protected private ledger is seeded
    personal = groups[0]
    assert personal["is_default"] and personal["default_expense_type_id"]
    # default group is protected
    admin.delete(f"/groups/{personal['id']}", expect=409)
    admin.post(f"/groups/{personal['id']}/members", {"display_name": "X"}, expect=409)
    types = admin.get("/expense-types")["items"]
    routings = admin.get("/routings")["items"]
    assert [t["name"] for t in types] == [r["name"] for r in routings] == ["Personal"]


def test_full_flow(client, admin: Api):
    g = make_livexpense(admin)
    assert g["current_period"]["status"] == "open"
    code = admin.get(f"/groups/{g['id']}/join-code")["join_code"]

    # placeholder Bob, added before he has an account
    bob_ph = admin.post(f"/groups/{g['id']}/members", {"display_name": "Bob"}, expect=201)["user"]
    assert bob_ph["is_placeholder"]

    # Carol joins with the code
    carol = register(client, code, "carol")
    members = admin.get(f"/groups/{g['id']}/members")["items"]
    assert len(members) == 3

    today = date.today().isoformat()
    exp = admin.post(
        "/expenses",
        {"group_id": g["id"], "title": "Groceries", "merchant": "HEB", "amount": 9000, "expense_date": today,
         "payer_id": admin.user["id"], "split_method": "equal",
         "participants": [{"user_id": m["user"]["id"]} for m in members]},
        expect=201,
    )
    assert sorted(s["amount"] for s in exp["shares"]) == [3000, 3000, 3000]
    assert exp["permissions"]["can_edit"]

    # carol can view but not edit (not creator/payer/owner)
    carol.get(f"/expenses/{exp['id']}")
    carol.patch(f"/expenses/{exp['id']}", {"title": "x"}, expect=403)

    bal = admin.get(f"/groups/{g['id']}/balances")
    net = {m["user"]["id"]: m["outstanding_net"] for m in bal["members"]}
    assert net[admin.user["id"]] == 6000 and net[carol.user["id"]] == -3000 and net[bob_ph["id"]] == -3000

    # carol ticks her own share; she can't tick Bob's
    carol.post(f"/expenses/{exp['id']}/shares/{carol.user['id']}/check")
    carol.post(f"/expenses/{exp['id']}/shares/{bob_ph['id']}/check", expect=403)

    # Bob pays via a settlement that links his share
    s = admin.post(
        "/settlements",
        {"group_id": g["id"], "from_user_id": bob_ph["id"], "to_user_id": admin.user["id"], "amount": 3000,
         "settled_on": today, "linked_shares": [{"expense_id": exp["id"], "user_id": bob_ph["id"]}]},
        expect=201,
    )
    assert s["linked_shares"]
    bal = admin.get(f"/groups/{g['id']}/balances")
    assert all(m["outstanding_net"] == 0 for m in bal["members"])
    assert admin.get(f"/expenses/{exp['id']}")["fully_checked"]

    # deleting the settlement unticks Bob's share
    admin.delete(f"/settlements/{s['id']}", expect=204)
    assert not admin.get(f"/expenses/{exp['id']}")["fully_checked"]

    # Bob registers and claims his placeholder → history moves with him (same id)
    preview = client.get(f"/api/v1/auth/join-preview?code={code}").json()
    assert [p["display_name"] for p in preview["placeholders"]] == ["Bob"]
    bob = register(client, code, "bob", "Bob", claim=bob_ph["id"])
    assert bob.user["id"] == bob_ph["id"]
    mine = bob.get("/expenses?involving=me")["items"]
    assert [e["id"] for e in mine] == [exp["id"]]
    assert len(bob.get("/groups")["items"]) == 2  # Personal + Livexpense


def test_guest_and_merge_into_existing(client, admin: Api):
    g = make_livexpense(admin)
    code = admin.get(f"/groups/{g['id']}/join-code")["join_code"]
    dave = register(client, code, "dave")
    # a guest placeholder only on one bill (not a group member)
    guest = admin.post("/placeholders", {"display_name": "Eve"}, expect=201)
    exp = admin.post(
        "/expenses",
        {"group_id": g["id"], "title": "Dinner", "amount": 6000, "expense_date": date.today().isoformat(),
         "payer_id": admin.user["id"], "split_method": "exact",
         "participants": [{"user_id": admin.user["id"], "amount": 2000}, {"user_id": dave.user["id"], "amount": 2000},
                          {"user_id": guest["id"], "amount": 2000}]},
        expect=201,
    )
    eve_share = next(s for s in exp["shares"] if s["user"]["id"] == guest["id"])
    assert eve_share["is_guest"]
    assert len(admin.get(f"/groups/{g['id']}/members")["items"]) == 2

    # Eve already has an account elsewhere: she joins with the code and merges her placeholder
    other = admin.post("/groups", {"name": "Other", "currency": "USD"}, expect=201)
    other_code = admin.get(f"/groups/{other['id']}/join-code")["join_code"]
    eve = register(client, other_code, "eve")
    eve.post("/groups/join", {"join_code": code, "claim_placeholder_id": guest["id"]})
    got = eve.get(f"/expenses/{exp['id']}")
    assert any(s["user"]["id"] == eve.user["id"] and s["amount"] == 2000 for s in got["shares"])


def test_resolve_rules_and_group_default_type(admin: Api):
    g = make_livexpense(admin)
    types = {"Life Expense": make_type(admin, "Life Expense", g["id"]), "Reimbursement": make_type(admin, "Reimbursement")}
    admin.post("/auto-rules", {"pattern": "heb", "set_type_id": types["Life Expense"]["id"]}, expect=201)

    r = admin.post("/expenses/resolve", {"merchant": "H-E-B #123 HEB GROCERY"})
    assert r["source"] == "rule" and r["group_id"] == g["id"] and r["expense_type_id"] == types["Life Expense"]["id"]

    # picking a group manually → that group's default type for me
    admin.put(f"/groups/{g['id']}/my-settings", {"default_expense_type_id": types["Reimbursement"]["id"]})
    r = admin.post("/expenses/resolve", {"merchant": "HEB", "group_id": g["id"]})
    assert r["group_id"] == g["id"] and r["expense_type_id"] == types["Reimbursement"]["id"]

    # nothing matches → default group
    r = admin.post("/expenses/resolve", {"merchant": "Unknown"})
    assert r["group_id"] == admin.get("/me")["default_group_id"]
    assert admin.post("/auto-rules/test", {"merchant": "heb"})["matched_rule"]


def test_period_rollover_freeze(client, admin: Api):
    from app.db import SessionLocal
    from app.models import Group, Period
    from app.services.periods import rollover_group

    g = make_livexpense(admin)
    code = admin.get(f"/groups/{g['id']}/join-code")["join_code"]
    carol = register(client, code, "carol")
    p0 = g["current_period"]
    start = date.fromisoformat(p0["starts_on"])
    end = date.fromisoformat(p0["ends_on"])
    exp = admin.post(
        "/expenses",
        {"group_id": g["id"], "title": "Rent", "amount": 10000, "expense_date": start.isoformat(),
         "payer_id": admin.user["id"], "participants": [{"user_id": admin.user["id"]}, {"user_id": carol.user["id"]}]},
        expect=201,
    )
    from datetime import UTC, datetime, time

    with SessionLocal() as db:
        grp = db.get(Group, g["id"])
        # 1 day after the cutoff → grace
        rollover_group(db, grp, datetime.combine(end + timedelta(days=2), time(12), UTC))
        db.commit()
        assert db.get(Period, p0["id"]).status == "grace"
    # bills dated in the grace period still go to the old period
    late = admin.post(
        "/expenses",
        {"group_id": g["id"], "title": "Late", "amount": 1000, "expense_date": end.isoformat(),
         "payer_id": admin.user["id"], "participants": [{"user_id": admin.user["id"]}]},
        expect=201,
    )
    assert late["period_id"] == p0["id"]
    with SessionLocal() as db:
        grp = db.get(Group, g["id"])
        rollover_group(db, grp, datetime.combine(end + timedelta(days=10), time(12), UTC))
        db.commit()
    periods = admin.get(f"/groups/{g['id']}/periods")["items"]
    assert periods[-1]["status"] == "frozen" and periods[0]["status"] == "open"

    bal = admin.get(f"/periods/{p0['id']}/balances")
    assert bal["snapshot"]["transfers"] == [
        {"from_user_id": carol.user["id"], "to_user_id": admin.user["id"], "amount": 5000}
    ]
    # frozen: no edits, no new bills dated inside it
    admin.patch(f"/expenses/{exp['id']}", {"title": "x"}, expect=409)
    admin.post(
        "/expenses",
        {"group_id": g["id"], "title": "Too late", "amount": 100, "expense_date": start.isoformat(),
         "payer_id": admin.user["id"], "participants": [{"user_id": admin.user["id"]}]},
        expect=409,
    )
    # but ticking and paying off still work
    carol.post(f"/expenses/{exp['id']}/shares/{carol.user['id']}/check")
    bal = admin.get(f"/periods/{p0['id']}/balances")
    assert bal["progress"] == {"total_owed_at_freeze": 5000, "outstanding_now": 0}
    # new period starts at zero
    assert all(m["outstanding_net"] == 0 for m in admin.get(f"/groups/{g['id']}/balances")["members"])


def test_cut_period(admin: Api):
    g = make_livexpense(admin)
    new = admin.post(f"/groups/{g['id']}/periods/cut", {})
    assert new["status"] == "open"
    periods = admin.get(f"/groups/{g['id']}/periods")["items"]
    assert len(periods) == 2 and periods[1]["status"] in ("grace", "frozen")


def test_itemized_and_patch_keeps_checks(admin: Api, client):
    g = make_livexpense(admin)
    code = admin.get(f"/groups/{g['id']}/join-code")["join_code"]
    carol = register(client, code, "carol")
    a, c = admin.user["id"], carol.user["id"]
    exp = admin.post(
        "/expenses",
        {"group_id": g["id"], "title": "Dinner", "amount": 1100, "expense_date": date.today().isoformat(),
         "payer_id": a, "split_method": "itemized", "participants": [{"user_id": a}, {"user_id": c}],
         "items": [{"name": "Steak", "amount": 600, "participant_ids": [a]},
                   {"name": "Salad", "amount": 200, "participant_ids": [c]},
                   {"name": "Bread", "amount": 200}]},
        expect=201,
    )
    assert {s["user"]["id"]: s["amount"] for s in exp["shares"]} == {a: 770, c: 330}
    assert len(exp["items"]) == 3
    admin.post(f"/expenses/{exp['id']}/shares/{c}/check")
    upd = admin.patch(f"/expenses/{exp['id']}", {"title": "Dinner out"})
    assert next(s for s in upd["shares"] if s["user"]["id"] == c)["checked_at"]
    upd = admin.patch(f"/expenses/{exp['id']}", {"amount": 1200})
    assert not next(s for s in upd["shares"] if s["user"]["id"] == c)["checked_at"]
    prev = admin.post("/expenses/preview-split", {"amount": 1000, "split_method": "shares",
                                                  "participants": [{"user_id": a, "weight": "3"}, {"user_id": c, "weight": "1"}]})
    assert {x["user_id"]: x["amount"] for x in prev["shares"]} == {a: 750, c: 250}


def test_attachments_and_export(client, admin: Api):
    g = make_livexpense(admin)
    h = admin.h
    r = client.post("/api/v1/attachments", headers=h, files={"file": ("receipt.png", png_bytes(), "image/png")})
    assert r.status_code == 201, r.text
    att = r.json()
    r = client.post("/api/v1/attachments", headers=h, files={"file": ("bill.pdf", pdf_bytes(), "application/pdf")})
    assert r.status_code == 201, r.text
    pdf = r.json()
    bad = client.post("/api/v1/attachments", headers=h, files={"file": ("x.txt", b"hello", "text/plain")})
    assert bad.status_code == 415 and bad.json()["error"]["code"] == "unsupported_file_type"

    exp = admin.post(
        "/expenses",
        {"group_id": g["id"], "title": "Costco run", "merchant": "Costco", "amount": 12345,
         "expense_date": date.today().isoformat(), "payer_id": admin.user["id"],
         "participants": [{"user_id": admin.user["id"]}], "attachment_ids": [att["id"], pdf["id"]]},
        expect=201,
    )
    assert len(exp["attachments"]) == 2
    f = client.get(exp["attachments"][0]["file_url"], headers=h)
    assert f.status_code == 200 and f.content[:4] == b"\x89PNG"
    t = client.get(exp["attachments"][1]["thumb_url"], headers=h)
    assert t.status_code == 200 and t.content[:3] == b"\xff\xd8\xff"

    personal = admin.get("/me")["default_group_id"]
    admin.post(
        "/expenses",
        {"group_id": personal, "title": "Lunch", "amount": 1500, "expense_date": date.today().isoformat(),
         "payer_id": admin.user["id"], "participants": [{"user_id": admin.user["id"]}]},
        expect=201,
    )
    today = date.today()
    r = client.post(
        "/api/v1/exports", headers=h,
        json={"from": (today - timedelta(days=40)).isoformat(), "to": today.isoformat(), "group_ids": [g["id"], personal]},
    )
    assert r.status_code == 200, r.text
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = z.namelist()
    assert "expenses.xlsx" in names
    assert sum(n.startswith("attachments/") for n in names) == 2
    wb = load_workbook(io.BytesIO(z.read("expenses.xlsx")))
    assert wb.sheetnames[:2] == ["Summary", "Expenses"]
    ws = wb["Expenses"]
    rows = list(ws.iter_rows(min_row=2, values_only=True))
    assert {r[1] for r in rows} == {"Livexpense", "Personal"}  # several groups in one workbook
    costco = next(r for r in rows if r[4] == "Costco run")
    assert costco[7] == 123.45
    assert ws._images, "invoice thumbnail should be embedded"
    link_cells = [c for c in ws[2] + ws[3] if c.hyperlink]
    assert link_cells and link_cells[0].hyperlink.target.startswith("attachments/")


def test_auth_refresh_rotation_and_admin(client, admin: Api):
    r = client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin-password"})
    rt = r.json()["refresh_token"]
    r2 = client.post("/api/v1/auth/refresh", json={"refresh_token": rt})
    assert r2.status_code == 200
    # reuse of a rotated token is rejected and revokes the family
    assert client.post("/api/v1/auth/refresh", json={"refresh_token": rt}).status_code == 401
    assert client.post("/api/v1/auth/refresh", json={"refresh_token": r2.json()["refresh_token"]}).status_code == 401

    u = admin.post("/admin/users", {"username": "frank", "password": "temp-pass-1", "display_name": "Frank"}, expect=201)
    frank = login(client, "frank", "temp-pass-1")
    frank.get("/groups", expect=403)  # must change password
    frank.post("/auth/change-password", {"current_password": "temp-pass-1", "new_password": "new-pass-123"}, expect=204)
    frank.get("/groups")
    temp = admin.post(f"/admin/users/{u['id']}/reset-password")["temporary_password"]
    login(client, "frank", temp)

    err = client.get("/api/v1/groups").json()
    assert err["error"]["code"] == "unauthorized"
    assert client.get("/api/v1/auth/join-preview?code=NOPE-000000").json()["error"]["code"] == "invalid_join_code"


def test_patch_me_persists(admin: Api):
    admin.patch("/me", {"display_name": "Boss"})
    assert admin.get("/me")["display_name"] == "Boss"
