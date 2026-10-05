# BillSplit API Design

> Version: v1 · Base URL: `/api/v1` · Companion: [`design.md`](design.md)
> The live OpenAPI schema is served by the backend at `/api/v1/openapi.json` (Swagger UI at `/docs`). If this document and the OpenAPI schema disagree, **the OpenAPI schema wins**. Report the mismatch.

---

## 1. Conventions

### 1.1 Transport
- JSON requests and responses (`Content-Type: application/json`). File uploads use `multipart/form-data`. Exports return `application/zip`.
- Field names are `snake_case`.
- IDs are opaque strings (UUIDv7). Do not parse them.

### 1.2 Auth
- `Authorization: Bearer <access_token>` on every endpoint except those marked **public**.
- The access token lives 15 min. Refresh it with `POST /auth/refresh` using the refresh token (30 days, **rotating**: every refresh returns a new refresh token, and the old one is invalidated).
- If `user.must_change_password` is true, every endpoint except `/auth/change-password`, `/auth/logout`, and `GET /me` returns `403 must_change_password`.

### 1.3 Money, dates, time
- **Money is always an integer in minor units** (cents). `1234` with currency `CAD` means $12.34. Never send floats.
- Each group has one `currency` (ISO 4217). Expenses and settlements inherit it, and responses echo it for convenience.
- `*_date` / `*_on` fields are calendar dates `YYYY-MM-DD`. `*_at` fields are UTC timestamps in ISO-8601 (`2026-10-05T14:03:00Z`).
- Percentages are decimal strings with up to 2 decimals (`"33.33"`). Share weights are decimal strings (`"1"`, `"1.5"`).

### 1.4 Pagination
List endpoints that can grow use cursor pagination:
```
GET /expenses?limit=50&cursor=<opaque>
→ { "items": [...], "next_cursor": "..." | null }
```
`limit` defaults to 50, max 200. Small fixed lists (groups, routings, types, rules, members) are returned unpaginated as `{ "items": [...] }`.

### 1.5 Errors
Every error response has this shape:
```json
{ "error": { "code": "period_frozen", "message": "Period 2026-09 is frozen", "details": { } } }
```

| HTTP | `code` | When |
|---|---|---|
| 400 | `validation_error` | Schema/field validation failed. `details.fields` maps field → message |
| 400 | `split_mismatch` | Exact amounts don't sum to the total, percents ≠ 100, or items exceed the total |
| 400 | `currency_locked` | Changing a group's currency after it has expenses |
| 401 | `unauthorized` | Missing, invalid, or expired token |
| 401 | `invalid_credentials` | Wrong username or password |
| 403 | `forbidden` | Not a member, or lacking the required role |
| 403 | `must_change_password` | See §1.2 |
| 403 | `account_disabled` | |
| 404 | `not_found` | Also returned for resources in groups you are not a member of |
| 404 | `invalid_join_code` | Code unknown, regenerated, or joining disabled |
| 409 | `username_taken` | |
| 409 | `already_member` | |
| 409 | `period_frozen` | Writing to a frozen period (or `expense_date` falls in one) |
| 409 | `default_group_protected` | Deleting, archiving, or adding members to a default group |
| 409 | `in_use` | Deleting a routing or type that is still referenced |
| 413 | `file_too_large` | > `UPLOAD_MAX_MB` (default 10) |
| 415 | `unsupported_file_type` | Not JPEG/PNG/WebP/HEIC/PDF (checked by content) |
| 422 | `ocr_failed` | OCR could not read the file |
| 429 | `ocr_busy` | OCR queue full. Retry later |
| 429 | `rate_limited` | Too many login attempts |
| 503 | `ocr_disabled` | |

---

## 2. Core Objects

### User (public profile)
```json
{ "id": "u_..", "display_name": "Bob", "username": "bob" , "is_placeholder": false }
```
`username` is `null` for placeholders.

### Me
```json
{
  "id": "u_..", "username": "liam", "display_name": "Liam", "email": null,
  "is_super_admin": false, "must_change_password": false,
  "default_group_id": "g_..", "created_at": "..."
}
```

### Group
```json
{
  "id": "g_..", "name": "Livexpense", "description": "Unit 4B", "currency": "CAD",
  "is_default": false, "is_recurring": true,
  "recurrence": { "cadence": "monthly", "cadence_n": null, "anchor_day": 1,
                  "timezone": "America/Toronto", "grace_days": 3 },
  "my_role": "owner",
  "member_count": 4,
  "current_period": { "id": "p_..", "starts_on": "2026-10-01", "ends_on": "2026-10-31", "status": "open" },
  "join_enabled": true,
  "archived_at": null, "created_at": "..."
}
```
- `recurrence` is `null` when `is_recurring` is false.
- `cadence` is one of `monthly`, `weekly`, or `every_n_days` (uses `cadence_n`).
- `anchor_day` is the day of the month (1–28) for monthly, the weekday (1=Mon … 7=Sun) for weekly, or ignored for `every_n_days` (the anchor is the first period's start).

### Member
```json
{ "user": { User }, "role": "owner|admin|member", "joined_at": "...", "left_at": null }
```

### Period
```json
{
  "id": "p_..", "group_id": "g_..", "starts_on": "2026-09-01", "ends_on": "2026-09-30",
  "grace_until": "2026-10-03T04:00:00Z", "status": "open|grace|frozen", "frozen_at": null
}
```
For a non-recurring group, `ends_on` and `grace_until` are `null`, and the status is always `open`.

### Routing / ExpenseType / AutoRule (all per-user, private to the caller)
```json
{ "id": "r_..", "name": "Life Expense", "group_id": "g_..", "archived_at": null }
{ "id": "t_..", "name": "Groceries", "routing_id": "r_..", "icon": "cart", "color": "#4caf50", "archived_at": null }
{
  "id": "ar_..", "priority": 10, "enabled": true,
  "field": "merchant", "mode": "contains|equals|regex", "pattern": "costco",
  "set_type_id": "t_..", "set_group_id": null
}
```

### Expense
```json
{
  "id": "e_..", "group_id": "g_..", "period_id": "p_..", "currency": "CAD",
  "title": "Weekly groceries", "merchant": "Costco", "amount": 15342,
  "expense_date": "2026-10-04",
  "expense_type": { "id": "t_..", "name": "Groceries", "owner_id": "u_.." },
  "payer": { User },
  "split_method": "equal|exact|percent|shares|itemized",
  "shares": [
    { "user": { User }, "is_guest": false, "split_value": null, "amount": 5114,
      "checked_at": null, "checked_by": null, "settlement_id": null }
  ],
  "items": [ { "id": "i_..", "name": "Milk", "amount": 599, "participant_ids": ["u_a","u_b"] } ],
  "attachments": [ { Attachment } ],
  "notes": null,
  "ocr_result": { ... } ,
  "fully_checked": false,
  "permissions": { "can_edit": true, "can_delete": true },
  "created_by": { User }, "created_at": "...", "updated_at": "..."
}
```
- `items` is present only for `itemized` bills (otherwise `[]`).
- `expense_type` is the **creator's** type. Other viewers see its name as a label only.
- `split_value` echoes the input for that participant: the exact amount (int), the percent string, or the weight string. It is `null` for equal and itemized.
- The payer's own share (if the payer participates) is reported but is never "owed". It is excluded from `fully_checked`.

### Settlement
```json
{
  "id": "s_..", "group_id": "g_..", "period_id": "p_..", "currency": "CAD",
  "from_user": { User }, "to_user": { User }, "amount": 4200, "settled_on": "2026-10-05",
  "note": "e-transfer", "linked_shares": [ { "expense_id": "e_..", "user_id": "u_.." } ],
  "attachments": [ { Attachment } ], "created_by": { User }, "created_at": "..."
}
```

### Attachment
```json
{
  "id": "a_..", "original_name": "IMG_2231.jpg", "mime": "image/jpeg", "size": 834221,
  "file_url": "/api/v1/attachments/a_../file", "thumb_url": "/api/v1/attachments/a_../thumb",
  "uploaded_by": { User }, "created_at": "..."
}
```
`file_url` and `thumb_url` require auth. Fetch them with the Bearer header (e.g. into a blob URL). For R2-backed files the server responds `302` to a short-lived presigned URL. Browsers follow it automatically, but the fetch must allow redirects.

---

## 3. Endpoints

Legend: 🔓 public · 👤 any logged-in user · 👥 group member · 🛡 group owner/admin · 👑 group owner · ⭐ super admin

### 3.1 Auth

| Method | Path | Who | Purpose |
|---|---|---|---|
| GET | `/auth/join-preview?code=LIVE-7K2Q9M` | 🔓 | See which group a code belongs to and its unclaimed placeholders |
| POST | `/auth/register` | 🔓 | Create an account with a join code |
| POST | `/auth/login` | 🔓 | |
| POST | `/auth/refresh` | 🔓 | Rotate tokens |
| POST | `/auth/logout` | 👤 | Revoke the refresh token |
| POST | `/auth/change-password` | 👤 | |

**`GET /auth/join-preview`** →
```json
{ "group": { "id": "g_..", "name": "Livexpense", "member_count": 4 },
  "placeholders": [ { "id": "u_..", "display_name": "Bob" } ] }
```

**`POST /auth/register`**
```json
{ "join_code": "LIVE-7K2Q9M", "username": "bob", "password": "≥8 chars",
  "display_name": "Bob", "email": null, "claim_placeholder_id": "u_.." }
```
- If `claim_placeholder_id` is set (it must be one from the preview), the placeholder is **converted** into this account. Its history is kept.
- The new user always gets the seed groups (Personal, Life Expense, Reimbursement, see design §6.3) and joins the code's group.
- Returns `201` with the same body as login.

**`POST /auth/login`** `{ "username", "password" }` →
```json
{ "access_token": "...", "refresh_token": "...", "token_type": "bearer",
  "expires_in": 900, "user": { Me } }
```

**`POST /auth/refresh`** `{ "refresh_token" }` → same as login.
**`POST /auth/logout`** `{ "refresh_token" }` → `204`.
**`POST /auth/change-password`** `{ "current_password", "new_password" }` → `204`. This clears `must_change_password`.

### 3.2 Me & People

| Method | Path | Who | Purpose |
|---|---|---|---|
| GET | `/me` | 👤 | Current user |
| PATCH | `/me` | 👤 | `{display_name?, email?}` |
| GET | `/me/overview` | 👤 | Balances across all groups |
| GET | `/people?q=bo` | 👤 | People I can add to a bill: members of my groups + placeholders I created. For a guest picker |
| POST | `/placeholders` | 👤 | `{display_name, group_id?}` creates a placeholder. If `group_id` is given (and the caller is a member), it is also added as a member |

**`GET /me/overview`** →
```json
{
  "totals": [ { "currency": "CAD", "i_owe": 4200, "owed_to_me": 9100, "net": 4900 } ],
  "groups": [
    { "group_id": "g_..", "group_name": "Livexpense", "currency": "CAD",
      "periods": [ { "period_id": "p_..", "label": "2026-09", "status": "frozen",
                     "i_owe": 0, "owed_to_me": 9100, "net": 9100 } ] }
  ]
}
```
Only periods with a non-zero outstanding amount for me, plus the current open period, are included.

### 3.3 Super Admin ⭐

| Method | Path | Purpose |
|---|---|---|
| GET | `/admin/users` | List all users (incl. placeholders, disabled) |
| POST | `/admin/users` | `{username, password, display_name}`. Creates a user directly (`must_change_password=true`) |
| POST | `/admin/users/{id}/reset-password` | → `{ "temporary_password": "..." }` and sets `must_change_password` |
| POST | `/admin/users/{id}/disable` / `/enable` | |

### 3.4 Groups

| Method | Path | Who | Purpose |
|---|---|---|---|
| GET | `/groups?include_archived=false` | 👤 | My groups |
| POST | `/groups` | 👤 | Create a group (caller becomes owner) |
| GET | `/groups/{id}` | 👥 | |
| PATCH | `/groups/{id}` | 🛡 | Name, description, currency (until the first expense), recurrence, `join_enabled` |
| DELETE | `/groups/{id}` | 👑 | Archive (soft). Not allowed for the default group |
| POST | `/groups/{id}/unarchive` | 👑 | |
| POST | `/groups/{id}/transfer-ownership` | 👑 | `{user_id}` (must be a registered member) |
| GET | `/groups/{id}/join-code` | 🛡 | → `{ "join_code": "LIVE-7K2Q9M", "join_enabled": true }` |
| POST | `/groups/{id}/join-code/regenerate` | 🛡 | New code; the old one stops working |
| POST | `/groups/join` | 👤 | `{join_code, claim_placeholder_id?}`. Existing user joins. A claim **merges** the placeholder into the caller |
| GET | `/groups/{id}/members?include_left=false` | 👥 | |
| POST | `/groups/{id}/members` | 🛡 | `{user_id}` or `{display_name}` (creates a placeholder member) |
| PATCH | `/groups/{id}/members/{user_id}` | 🛡 | `{role: "admin"|"member"}` (owner changes via transfer) |
| DELETE | `/groups/{id}/members/{user_id}` | 🛡 | Remove (soft). History stays |
| POST | `/groups/{id}/leave` | 👥 | The owner must transfer ownership first |
| GET | `/groups/{id}/placeholders` | 👥 | Unclaimed placeholders tied to this group (members + guests on its bills) |
| POST | `/groups/{id}/placeholders/{pid}/claim` | 👥 | Caller claims the placeholder: **merge** into the caller |

**`POST /groups`**
```json
{ "name": "Livexpense", "description": "Unit 4B", "currency": "CAD",
  "is_recurring": true,
  "recurrence": { "cadence": "monthly", "anchor_day": 1, "timezone": "America/Toronto", "grace_days": 3 } }
```
For a recurring group the first period starts at the most recent anchor on or before today (e.g. created Oct 5 → period Oct 1–31).

**Recurrence change via PATCH**: takes effect from the **next** period. The response includes `"recurrence_effective_from": "2026-11-01"`.

### 3.5 Periods & Balances

| Method | Path | Who | Purpose |
|---|---|---|---|
| GET | `/groups/{id}/periods` | 👥 | Newest first, paginated |
| GET | `/periods/{id}` | 👥 | Period + member snapshot |
| POST | `/groups/{id}/periods/cut` | 🛡 | End the current period now. It goes to `grace`, and a new `open` period starts today |
| GET | `/periods/{id}/balances` | 👥 | Balances (live) + snapshot (if frozen) |
| GET | `/groups/{id}/balances` | 👥 | Shortcut: balances for the current period |

**`GET /periods/{id}/balances`** →
```json
{
  "period": { Period }, "currency": "CAD",
  "members": [
    { "user": { User }, "is_guest": false,
      "paid": 30000, "owed": 12000, "settled_in": 0, "settled_out": 0,
      "outstanding_net": 18000 }
  ],
  "pairwise": [ { "from_user_id": "u_b", "to_user_id": "u_a", "amount": 6000 } ],
  "suggested_transfers": [ { "from_user_id": "u_b", "to_user_id": "u_a", "amount": 6000 } ],
  "snapshot": {
    "frozen_at": "2026-10-04T04:00:00Z",
    "balances": [ { "user_id": "u_a", "paid": 30000, "owed": 12000, "net": 18000 } ],
    "transfers": [ { "from_user_id": "u_b", "to_user_id": "u_a", "amount": 6000 } ]
  },
  "progress": { "total_owed_at_freeze": 18000, "outstanding_now": 6000 }
}
```
- `snapshot` and `progress` are `null` unless the status is `frozen`.
- `pairwise` and `outstanding_net` are **live**: they shrink as shares are checked and settlements recorded. The snapshot never changes.
- Sign convention: `outstanding_net > 0` means the person is owed money. `< 0` means they owe.

### 3.6 Routings, Expense Types, Auto-Rules (per-user)

| Method | Path | Purpose |
|---|---|---|
| GET/POST | `/routings` | `{name, group_id?}` |
| PATCH/DELETE | `/routings/{id}` | DELETE → `409 in_use` if types still point to it (archive by `PATCH {archived: true}`) |
| GET/POST | `/expense-types` | `{name, routing_id, icon?, color?}` |
| PATCH/DELETE | `/expense-types/{id}` | DELETE of a type used by expenses archives it instead |
| GET/POST | `/auto-rules` | `{field, mode, pattern, set_type_id?, set_group_id?, enabled?}` (at least one `set_*`) |
| PATCH/DELETE | `/auto-rules/{id}` | |
| PUT | `/auto-rules/order` | `{ "ids": ["ar_1","ar_3","ar_2"] }` rewrites priorities in this order |
| POST | `/auto-rules/test` | `{merchant?, title?}` → which rule matches (for a rule editor preview) |

`group_id` on a routing or `set_group_id` on a rule must be a group the caller belongs to. If the caller later leaves that group, routing resolution falls back to the default group.

### 3.7 Expenses

| Method | Path | Who | Purpose |
|---|---|---|---|
| POST | `/expenses/resolve` | 👤 | Suggest type + group from merchant/title/type |
| POST | `/expenses/preview-split` | 👤 | Compute shares without saving |
| GET | `/expenses` | 👤 | List/filter |
| POST | `/expenses` | 👥 (target group) | Create |
| GET | `/expenses/{id}` | 👥 or guest on the bill | |
| PATCH | `/expenses/{id}` | creator / payer / group owner | Update |
| DELETE | `/expenses/{id}` | creator / payer / group owner | Soft delete |
| POST | `/expenses/{id}/shares/{user_id}/check` | payer or that debtor | Mark paid |
| DELETE | `/expenses/{id}/shares/{user_id}/check` | payer or that debtor | Unmark |
| POST | `/expenses/{id}/attachments` | 👥 | `{attachment_ids: [...]}` link uploaded files (allowed in frozen periods) |
| DELETE | `/expenses/{id}/attachments/{aid}` | creator / payer / owner / uploader | Unlink |

**`POST /expenses/resolve`**
```json
{ "merchant": "COSTCO WHOLESALE #123", "title": null, "expense_type_id": null }
```
→
```json
{ "expense_type_id": "t_..", "group_id": "g_..", "routing_id": "r_..",
  "source": "rule|type|default", "matched_rule_id": "ar_.." }
```

**`POST /expenses`**
```json
{
  "group_id": "g_..",
  "title": "Weekly groceries",
  "merchant": "Costco",
  "amount": 15342,
  "expense_date": "2026-10-04",
  "expense_type_id": "t_..",
  "payer_id": "u_a",
  "split_method": "equal",
  "participants": [
    { "user_id": "u_a" },
    { "user_id": "u_b" },
    { "user_id": "u_guest" }
  ],
  "items": [],
  "notes": null,
  "attachment_ids": ["a_.."],
  "ocr_result": null
}
```
- `participants`: who shares the bill. **The frontend pre-fills all current group members, and the user can untick members or add guests.** Any participant not currently in the group becomes a guest (`is_guest: true`). To add a brand-new person, call `POST /placeholders` first, then use the returned id.
- `payer_id` may be any participant or member (it does not need to be a participant).
- Per-method participant fields:

| `split_method` | participant field | rule |
|---|---|---|
| `equal` | — | |
| `exact` | `"amount": 5000` (int cents) | Σ = `amount` |
| `percent` | `"percent": "33.33"` | Σ = 100.00 |
| `shares` | `"weight": "2"` | each > 0 |
| `itemized` | — (use `items`) | |

- `itemized`: `items: [{ "name": "Milk", "amount": 599, "participant_ids": ["u_a","u_b"] }]`. Requires `Σ items.amount ≤ amount`. The difference (tax/tip) is spread proportionally to each person's item subtotal. Items with `participant_ids: []` are split among all participants. Item participants must also be in `participants`.
- `period_id` is **derived** from `expense_date`. If that date falls in a frozen period you get `409 period_frozen`.
- Returns `201 { Expense }`.

**`POST /expenses/preview-split`**: the same body as create (only `amount`, `split_method`, `participants`, `items`, and `payer_id` matter) → `{ "shares": [ { "user_id", "amount" } ] }`. Use it to show live per-person amounts as the user edits.

**`PATCH /expenses/{id}`**: send any subset of the create fields. If `split_method`, `participants`, `items`, or `amount` change, the shares are **recomputed** and existing check marks are kept for participants whose share amount is unchanged (others are unchecked). `attachment_ids`, if sent, **replaces** the linked set.

**`GET /expenses` query params**

| Param | Notes |
|---|---|
| `group_id` | Repeatable. Default: all my groups |
| `period_id` | |
| `from`, `to` | `expense_date` range, inclusive |
| `involving` | `me` (I'm payer or participant) or `any` (default, all visible) |
| `expense_type_id` | Only meaningful for my own types |
| `checked` | `true` / `false`: filter to bills fully (un)checked |
| `q` | Search in title, merchant, and notes |
| `limit`, `cursor` | |
| `sort` | `-expense_date` (default), `expense_date`, `-amount`, `-created_at` |

Each list item is the full Expense object minus `ocr_result`.

### 3.8 Settlements

| Method | Path | Who | Purpose |
|---|---|---|---|
| GET | `/settlements?group_id=&period_id=&involving=me` | 👤 | |
| POST | `/settlements` | 👥 (from or to party, or 🛡) | Record a payment |
| GET | `/settlements/{id}` | 👥 | |
| PATCH | `/settlements/{id}` | creator / from / to | |
| DELETE | `/settlements/{id}` | creator / from / to | Unchecks shares that were checked by this settlement |
| POST | `/settlements/{id}/attachments` | 👥 | `{attachment_ids}` |

**`POST /settlements`**
```json
{ "group_id": "g_..", "period_id": "p_..", "from_user_id": "u_b", "to_user_id": "u_a",
  "amount": 6000, "settled_on": "2026-10-05", "note": "e-transfer",
  "linked_shares": [ { "expense_id": "e_1", "user_id": "u_b" } ],
  "attachment_ids": [] }
```
- `period_id` is optional. The default is the period containing `settled_on`. Settlements **are allowed in frozen periods** (paying off an old month).
- `linked_shares`: shares this payment covers. They must be `from_user`'s shares on bills paid by `to_user`. They are auto-checked. If `linked_shares` is empty, the amount reduces the pairwise balance directly.
- Helper: `GET /periods/{id}/balances` → `pairwise` tells the UI how much is owed. The UI can also list unchecked shares via `GET /expenses?period_id=..&involving=me&checked=false`.

### 3.9 Attachments

| Method | Path | Who | Purpose |
|---|---|---|---|
| POST | `/attachments` | 👤 | `multipart/form-data`, field `file`. Upload one file → `201 { Attachment }` (unlinked) |
| GET | `/attachments/{id}` | linked-group member / uploader | Metadata |
| GET | `/attachments/{id}/file` | same | The file (`302` to R2 presigned URL, or streamed) |
| GET | `/attachments/{id}/thumb` | same | JPEG thumbnail (≤ 400 px) |
| DELETE | `/attachments/{id}` | uploader / group owner | Soft delete |

Accepted: JPEG, PNG, WebP, HEIC/HEIF (stored as JPEG), and PDF, up to 10 MB each. There is no count limit. The typical flow is to upload first and then pass `attachment_ids` when creating or updating the expense or settlement.

### 3.10 OCR

**`POST /ocr/receipt`** 👤: `multipart/form-data` with `file` (image or PDF) and optional `keep=true`.
```json
{
  "merchant": "COSTCO WHOLESALE",
  "total": 15342,
  "currency_guess": "CAD",
  "date": "2026-10-04",
  "confidence": { "merchant": 0.82, "total": 0.91, "date": 0.65 },
  "raw_lines": [ { "text": "TOTAL 153.42", "confidence": 0.97 } ],
  "suggestion": { "expense_type_id": "t_..", "group_id": "g_..", "source": "rule", "matched_rule_id": "ar_.." },
  "attachment": { Attachment }
}
```
- Any field may be `null`. **Always treat the output as a pre-fill that the user confirms.**
- `attachment` is present only when `keep=true`. Pass its id in `attachment_ids` when saving the expense.
- Typical latency is 1–3 s. It may take longer if other OCR jobs are queued. Expect `429 ocr_busy` or `503 ocr_disabled`.

### 3.11 Export

**`POST /exports`** 👤
```json
{ "from": "2026-09-01", "to": "2026-09-30", "group_ids": ["g_.."], "include_settlements": true }
```
- `group_ids` omitted means all my groups.
- Rows are bills where the caller is the payer or a participant.
- Response: `200 application/zip`, `Content-Disposition: attachment; filename="billsplit-export-2026-09-01_2026-09-30.zip"`. The ZIP contains `expenses.xlsx` and `attachments/`.
- It is synchronous. Use a long request timeout (≈ 60 s) and show a spinner.

### 3.12 Misc

| Method | Path | Who | Purpose |
|---|---|---|---|
| GET | `/health` | 🔓 | `{status, version, ocr_enabled, storage: "r2"|"local"}` |
| GET | `/meta/currencies` | 👤 | Supported ISO codes with minor-unit digits (e.g. JPY = 0) |

> **Minor units vary by currency.** Use `/meta/currencies` → `minor_units` to format amounts. JPY has 0 decimals, so `1500` = ¥1500.

---

## 4. Typical Flows

### 4.1 Onboarding a new roommate who was a placeholder
1. The owner opens **Livexpense** → `GET /groups/{id}/join-code` and shares `LIVE-7K2Q9M`.
2. The roommate opens the app → `GET /auth/join-preview?code=LIVE-7K2Q9M` → sees "Livexpense" and placeholder "Bob".
3. `POST /auth/register` with `claim_placeholder_id = <Bob>` → logged in, with all of Bob's history attached.

### 4.2 Adding a bill from a receipt photo
1. `POST /ocr/receipt` (file, `keep=true`) → pre-fill merchant, total, date, type, and group.
2. The user tweaks the values. The participant picker is pre-filled from `GET /groups/{group_id}/members` (current, not left).
3. While editing → `POST /expenses/preview-split` for live per-person amounts.
4. `POST /expenses` with `attachment_ids: [ocr.attachment.id]`.

### 4.3 Month-end
- The scheduler moves Sep to `grace` on Oct 1 and to `frozen` on Oct 4. The UI shows the period badge from `Group.current_period` and `GET /groups/{id}/periods`.
- To pay off September: `GET /periods/{sep}/balances` → `suggested_transfers` → `POST /settlements` (optionally linking shares).

---

## 5. Changelog
- v1 (2026-10-05): initial.
