"""Per-user group order and the preferred (first) group for new expenses."""

import json
import sqlite3
from datetime import date

import pytest
from fastapi.testclient import TestClient

from tests.conftest import Api, login, png_bytes, register


def new_group(api: Api, name: str) -> dict:
    return api.post("/groups", {"name": name, "currency": "USD"}, expect=201)


def code_of(api: Api, gid: str) -> str:
    return api.get(f"/groups/{gid}/join-code")["join_code"]


def order(api: Api) -> dict:
    return api.get("/me/group-order")


def put(api: Api, ids, revision, expect=200):
    return api.put("/me/group-order", {"group_ids": ids, "revision": revision}, expect=expect)


def to_front(o: dict, gid: str) -> list[str]:
    return [gid] + [g for g in o["group_ids"] if g != gid]


@pytest.fixture
def shared(client, admin: Api):
    """admin owns 'Flat'; carol is a plain member."""
    g = new_group(admin, "Flat")
    carol = register(client, code_of(admin, g["id"]), "carol")
    return g, carol


# 1 ---------------------------------------------------------------- per-user, persisted, plain members allowed


def test_orders_are_per_user_and_persist(client, admin: Api, shared, tmp_path):
    g, carol = shared
    a0, c0 = order(admin), order(carol)
    assert a0["preferred_group_id"] == a0["group_ids"][0] == admin.get("/me")["default_group_id"]

    # carol (plain member) moves the shared group first; admin moves it last
    c1 = put(carol, to_front(c0, g["id"]), c0["revision"])
    assert c1["preferred_group_id"] == g["id"] and c1["revision"] == c0["revision"] + 1
    a_ids = [x for x in a0["group_ids"] if x != g["id"]] + [g["id"]]
    a1 = put(admin, a_ids, a0["revision"])
    assert a1["group_ids"][-1] == g["id"]
    # each user's order is unaffected by the other
    assert order(carol)["group_ids"] == c1["group_ids"]
    assert order(admin)["group_ids"] == a_ids

    # survives a new login (another device) and a server restart
    carol2 = login(client, "carol", "password123")
    assert order(carol2) == c1
    from app.main import create_app

    with TestClient(create_app()) as c2:
        carol3 = login(c2, "carol", "password123")
        assert order(carol3) == c1
        assert [x["id"] for x in carol3.get("/groups")["items"]] == c1["group_ids"]


# 2 ---------------------------------------------------------------- preferred group drives defaults; private ledger untouched


def test_preferred_group_drives_resolve_and_ocr(client, admin: Api, shared, monkeypatch):
    g, carol = shared
    me_before = carol.get("/me")
    personal = me_before["default_group_id"]
    o = order(carol)
    put(carol, to_front(o, g["id"]), o["revision"])

    me = carol.get("/me")
    assert me["preferred_group_id"] == g["id"]
    assert me["default_group_id"] == personal  # unchanged
    groups = {x["id"]: x for x in carol.get("/groups")["items"]}
    assert groups[personal]["is_default"] and not groups[g["id"]]["is_default"]
    assert carol.get("/groups")["items"][0]["id"] == g["id"]

    r = carol.post("/expenses/resolve", {"merchant": "Nothing matches"})
    assert r["group_id"] == g["id"] and r["source"] == "default"

    from app.api import attachments as att_api

    class FakeOCR:
        def read(self, data, mime):
            return {"merchant": "Nothing matches", "total": 1234, "currency_guess": "CNY", "date": "2026-10-04",
                    "confidence": {}, "raw_lines": []}

    monkeypatch.setattr(att_api, "get_ocr", lambda: FakeOCR())
    resp = client.post("/api/v1/ocr/receipt", headers=carol.h, files={"file": ("r.png", png_bytes(), "image/png")})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["suggestion"]["group_id"] == r["group_id"]
    assert body["currency_guess"] == "CNY" and body["total"] == 1234  # OCR values untouched

    # private ledger protections are unchanged
    carol.delete(f"/groups/{personal}", expect=409)
    carol.post(f"/groups/{personal}/leave", expect=409)
    carol.post(f"/groups/{personal}/members", {"display_name": "X"}, expect=409)


# 3 ---------------------------------------------------------------- priority and safe fallback


def test_resolve_priority_and_fallback(client, admin: Api, shared):
    g, carol = shared
    other = new_group(carol, "Trip")
    types = {t["name"]: t for t in carol.get("/expense-types")["items"]}
    routings = {r["name"]: r for r in carol.get("/routings")["items"]}
    o = order(carol)
    put(carol, to_front(o, other["id"]), o["revision"])  # preferred = Trip

    # preferred group brings my default type for it, without re-routing
    carol.put(f"/groups/{other['id']}/my-settings", {"default_expense_type_id": types["Reimbursement"]["id"]})
    r = carol.post("/expenses/resolve", {"merchant": "x"})
    assert r == {**r, "group_id": other["id"], "expense_type_id": types["Reimbursement"]["id"], "source": "default"}

    # explicit manual group wins and skips rules
    carol.post("/auto-rules", {"pattern": "heb", "set_group_id": g["id"]}, expect=201)
    r = carol.post("/expenses/resolve", {"merchant": "HEB", "group_id": carol.get("/me")["default_group_id"]})
    assert r["group_id"] == carol.get("/me")["default_group_id"]

    # merchant rule beats the preferred group
    r = carol.post("/expenses/resolve", {"merchant": "HEB"})
    assert r["group_id"] == g["id"] and r["source"] == "rule"

    # explicit type → routing beats the preferred group
    carol.patch(f"/routings/{routings['Life Expense']['id']}", {"group_id": g["id"]})
    r = carol.post("/expenses/resolve", {"expense_type_id": types["Life Expense"]["id"]})
    assert r["group_id"] == g["id"] and r["source"] == "type"

    # rule / routing target no longer available → preferred group
    carol.post(f"/groups/{g['id']}/leave", expect=204)
    assert carol.post("/expenses/resolve", {"merchant": "HEB"})["group_id"] == other["id"]
    r = carol.post("/expenses/resolve", {"expense_type_id": types["Life Expense"]["id"]})
    assert r["group_id"] == other["id"]

    # preferred group archived → next group becomes preferred
    carol.delete(f"/groups/{other['id']}", expect=204)
    o = order(carol)
    assert other["id"] not in o["group_ids"]
    assert carol.post("/expenses/resolve", {"merchant": "x"})["group_id"] == o["preferred_group_id"]
    assert o["preferred_group_id"] != other["id"]


# 4 ---------------------------------------------------------------- validation, no partial writes


def test_put_validation(client, admin: Api, shared):
    g, carol = shared
    stranger_group = new_group(admin, "Admin only")
    left = new_group(carol, "Left")
    archived = new_group(carol, "Old")
    carol.delete(f"/groups/{archived['id']}", expect=204)
    # transferring to yourself is a no-op (used to demote the owner)
    assert carol.post(f"/groups/{left['id']}/transfer-ownership", {"user_id": carol.user["id"]})["my_role"] == "owner"
    # make carol leave "Left": add admin as owner first
    code = code_of(carol, left["id"])
    admin.post("/groups/join", {"join_code": code})
    carol.post(f"/groups/{left['id']}/transfer-ownership", {"user_id": admin.user["id"]})
    carol.post(f"/groups/{left['id']}/leave", expect=204)

    o = order(carol)
    ids, rev = o["group_ids"], o["revision"]

    def err(resp):
        return resp["error"]["code"]

    assert err(put(carol, ids + [ids[0]], rev, expect=400)) == "validation_error"  # duplicate
    assert err(put(carol, ids[:-1], rev, expect=409)) == "group_order_conflict"  # missing
    assert err(put(carol, ids + ["no-such-group"], rev, expect=404)) == "not_found"  # unknown
    assert err(put(carol, ids + [stranger_group["id"]], rev, expect=404)) == "not_found"  # someone else's
    assert err(put(carol, ids + [left["id"]], rev, expect=404)) == "not_found"  # left
    conflict = put(carol, ids + [archived["id"]], rev, expect=409)  # archived (still a member)
    assert conflict["error"]["details"]["current"] == o
    assert err(carol.put("/me/group-order", {"group_ids": "nope", "revision": rev}, expect=400)) == "validation_error"
    assert order(carol) == o  # nothing was written

    # same order with the current revision is a no-op
    assert put(carol, ids, rev) == o
    # reordering bumps the revision; the old revision is then stale even with the same order
    new = put(carol, list(reversed(ids)), rev)
    assert new["revision"] == rev + 1
    assert err(put(carol, list(reversed(ids)), rev, expect=409)) == "group_order_conflict"


# 5 ---------------------------------------------------------------- concurrency


def test_concurrent_updates_and_membership_changes(client, admin: Api, shared):
    g, carol = shared
    phone, laptop = carol, login(client, "carol", "password123")
    o = order(phone)
    put(phone, to_front(o, g["id"]), o["revision"])
    stale = put(laptop, list(reversed(o["group_ids"])), o["revision"], expect=409)
    assert stale["error"]["details"]["current"]["preferred_group_id"] == g["id"]

    # a membership change between read and save invalidates the save
    o = order(laptop)
    new = new_group(admin, "Late")
    admin.post(f"/groups/{new['id']}/members", {"user_id": carol.user["id"]}, expect=201)
    resp = put(laptop, list(reversed(o["group_ids"])), o["revision"], expect=409)
    cur = resp["error"]["details"]["current"]
    assert cur["group_ids"][-1] == new["id"] and cur["revision"] == o["revision"] + 1


# 6 ---------------------------------------------------------------- lifecycle rules


def test_lifecycle(client, admin: Api, shared):
    g, carol = shared
    o0 = order(carol)
    admin_before = order(admin)

    # create → appended at the end, preferred unchanged; other members unaffected
    t = new_group(carol, "Trip")
    o1 = order(carol)
    assert o1["group_ids"] == o0["group_ids"] + [t["id"]] and o1["revision"] == o0["revision"] + 1
    assert order(admin) == admin_before

    # make Flat preferred
    o2 = put(carol, to_front(o1, g["id"]), o1["revision"])

    # join another group → appended
    j = new_group(admin, "Club")
    carol.post("/groups/join", {"join_code": code_of(admin, j["id"])})
    o3 = order(carol)
    assert o3["group_ids"] == o2["group_ids"] + [j["id"]] and o3["preferred_group_id"] == g["id"]

    # removed by an admin → dropped; first group removed → next becomes preferred
    admin.delete(f"/groups/{g['id']}/members/{carol.user['id']}", expect=204)
    o4 = order(carol)
    assert g["id"] not in o4["group_ids"] and o4["preferred_group_id"] == o2["group_ids"][1]
    assert o4["revision"] > o3["revision"]

    # re-join → appended at the end, does not take back first place
    carol.post("/groups/join", {"join_code": code_of(admin, g["id"])})
    o5 = order(carol)
    assert o5["group_ids"][-1] == g["id"] and o5["preferred_group_id"] == o4["preferred_group_id"]

    # archive (affects every member) → removed for both users, each revision bumped
    admin_mid = order(admin)
    carol_mid = order(carol)
    admin.delete(f"/groups/{j['id']}", expect=204)
    assert j["id"] not in order(admin)["group_ids"] and order(admin)["revision"] == admin_mid["revision"] + 1
    assert j["id"] not in order(carol)["group_ids"] and order(carol)["revision"] == carol_mid["revision"] + 1
    # include_archived lists archived groups after the available ones
    items = carol.get("/groups?include_archived=true")["items"]
    assert [x["id"] for x in items[:-1]] == order(carol)["group_ids"] and items[-1]["id"] == j["id"]

    # unarchive → appended at the end for each member
    admin.post(f"/groups/{j['id']}/unarchive")
    assert order(carol)["group_ids"][-1] == j["id"] and order(admin)["group_ids"][-1] == j["id"]

    # GET does not bump the revision
    r1 = order(carol)["revision"]
    assert order(carol)["revision"] == r1


def test_new_user_seed_order(client, admin: Api, shared):
    g, carol = shared
    o = order(carol)
    names = {x["id"]: x["name"] for x in carol.get("/groups")["items"]}
    assert [names[i] for i in o["group_ids"]] == ["Personal", "Life Expense", "Reimbursement", "Flat"]
    assert o["preferred_group_id"] == carol.get("/me")["default_group_id"]


def test_pairwise_is_full_period(client, admin: Api, shared):
    """pairwise lists every remaining counterparty relation in the period, not simplified transfers."""
    g, carol = shared
    dave = register(client, code_of(admin, g["id"]), "dave")
    ids = [admin.user["id"], carol.user["id"], dave.user["id"]]
    today = date.today().isoformat()
    # admin paid for carol; carol paid for dave → simplification could route dave→admin, pairwise must not
    admin.post("/expenses", {"group_id": g["id"], "title": "A", "amount": 1000, "expense_date": today,
                             "payer_id": ids[0], "split_method": "exact",
                             "participants": [{"user_id": ids[1], "amount": 1000}]}, expect=201)
    carol.post("/expenses", {"group_id": g["id"], "title": "B", "amount": 1000, "expense_date": today,
                             "payer_id": ids[1], "split_method": "exact",
                             "participants": [{"user_id": ids[2], "amount": 1000}]}, expect=201)
    bal = carol.get(f"/groups/{g['id']}/balances")
    pairs = {(p["from_user_id"], p["to_user_id"]): p["amount"] for p in bal["pairwise"]}
    assert pairs == {(ids[1], ids[0]): 1000, (ids[2], ids[1]): 1000}


# 7 ---------------------------------------------------------------- migration


def test_migration_initial_order_and_untouched_data(tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "m.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "migrations")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "f86ab736c428")  # schema before this feature

    con = sqlite3.connect(db_path)
    now = "2026-10-01T00:00:00.000000Z"
    con.executescript(f"""
        INSERT INTO users (id, username, display_name, is_placeholder, is_super_admin, must_change_password, created_at, updated_at)
          VALUES ('u1','u1','U1',0,0,0,'{now}','{now}'), ('ph','',  'PH',1,0,0,'{now}','{now}');
        INSERT INTO groups (id, name, currency, owner_id, is_default, join_enabled, is_recurring, timezone, grace_days, created_at, updated_at, archived_at) VALUES
          ('g-def','Personal','USD','u1',1,0,0,'UTC',3,'{now}','{now}',NULL),
          ('g-b','Same','USD','u1',0,1,0,'UTC',3,'{now}','{now}',NULL),
          ('g-a','Same','USD','u1',0,1,0,'UTC',3,'{now}','{now}',NULL),
          ('g-z','Alpha','USD','u1',0,1,0,'UTC',3,'{now}','{now}',NULL),
          ('g-arch','Archived','USD','u1',0,1,0,'UTC',3,'{now}','{now}','{now}'),
          ('g-left','Left','USD','u1',0,1,0,'UTC',3,'{now}','{now}',NULL);
        INSERT INTO group_members (group_id, user_id, role, joined_at, left_at) VALUES
          ('g-def','u1','owner','{now}',NULL), ('g-b','u1','owner','{now}',NULL), ('g-a','u1','owner','{now}',NULL),
          ('g-z','u1','owner','{now}',NULL), ('g-arch','u1','owner','{now}',NULL), ('g-left','u1','member','{now}','{now}'),
          ('g-b','ph','member','{now}',NULL);
        INSERT INTO periods (id, group_id, starts_on, status) VALUES ('p1','g-b','2026-10-01','open');
        INSERT INTO expenses (id, group_id, period_id, title, amount, expense_date, payer_id, split_method, created_by_id, created_at, updated_at)
          VALUES ('e1','g-b','p1','Rent',12345,'2026-10-02','u1','equal','u1','{now}','{now}');
        INSERT INTO expense_shares (expense_id, user_id, is_guest, amount) VALUES ('e1','u1',0,6173), ('e1','ph',0,6172);
    """)
    con.commit()
    before = {t: con.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall()
              for t in ("expenses", "expense_shares", "periods", "groups", "group_members")}
    con.close()

    command.upgrade(cfg, "head")

    con = sqlite3.connect(db_path)
    after = {t: con.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall() for t in before}
    assert after == before  # no bill, amount, group_id, period or membership changed
    prefs = {uid: (json.loads(o), rev) for uid, o, rev in
             con.execute("SELECT user_id, group_order, group_order_revision FROM user_preferences")}
    assert prefs == {"u1": (["g-def", "g-z", "g-a", "g-b"], 1)}  # default first, name, then id; no placeholders
