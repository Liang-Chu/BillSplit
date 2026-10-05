# BillSplit Backend — Design Document

> Status: **v1.2 — implemented** (backend v0.1.0). Companion docs: [`api.md`](api.md), [`frontend-handout.md`](frontend-handout.md).
> Last updated: 2026-10-05

---

## 1. Goals & Non-Goals

### Goals
- Internal, low-traffic expense management and bill-splitting backend. It replaces the current Splitwise workflow of creating a new group every month.
- Runs on a **2 GB RAM** AWS instance (e.g. `t4g.small`).
- **SQLite** is the only database. No external services are required to run it (Cloudflare R2 is optional).

### Non-Goals
- Real payment processing. We only *record* payments and *check off* shares.
- High concurrency, multi-region deployments, or public sign-up.
- 100% accurate OCR. Best effort is enough: store name + total (+ date).

---

## 2. Prior Art — Open-Source Starting Points

| Project | Stack | DB | Notes |
|---|---|---|---|
| [SplitPro](https://github.com/oss-apps/split-pro) | Next.js full-stack, Prisma | PostgreSQL | Mature split logic (equal / % / shares / exact / adjustments). UI and API tightly coupled. |
| [Spliit](https://spliit.pro) | Next.js, Prisma | PostgreSQL | Account-less groups via links. OCR uses GPT-4 Vision (paid). |
| [ShareTab](https://github.com/lolchocotaco/sharetab) | Next.js 16, tRPC, Prisma | PostgreSQL | LLM receipt scanning. Heavier. |
| [Chara](https://github.com/DowLucas/chara) | Go API + mobile apps | — | AGPLv3 (copyleft risk if forked). |
| [Splitwise-Clone](https://github.com/Shrinivas5/Splitwise-Clone) | FastAPI + SQLAlchemy | SQLite/PG | Toy-level; little to reuse. |

**Decision: build from scratch and reuse ideas, not code.** All mature options are Next.js + PostgreSQL monoliths. None of them supports our routing chain, rolling periods, placeholder claim/merge, or the Excel/ZIP export. We borrow these ideas: integer minor-unit money, the split-method set, deterministic remainder allocation, and the debt-simplification algorithm.

---

## 3. Feature Summary

| # | Feature | Section |
|---|---|---|
| F1 | Accounts without email; join by group code; placeholder people who can be claimed (or merged) later | §6.1 |
| F2 | Lightweight self-hosted OCR (store name, total, date), English + Chinese | §6.9 |
| F3 | Per-user expense types → routings → groups; auto-routing rules by store name; undeletable default group | §6.3, §6.4 |
| F4 | Groups; per-bill participant override (exclude members, add bill-only guests) | §6.2, §6.5 |
| F5 | Recurring groups: rolling periods with grace window, snapshot and freeze | §6.6 |
| F6 | No payments; check off shares; record settlements | §6.7 |
| F7 | Image/PDF attachments; R2 primary, local fallback | §6.10 |
| F8 | Excel export (ZIP bundle) filtered by person, time, and group | §6.11 |

---

## 4. Tech Stack

| Concern | Choice |
|---|---|
| Language / framework | **Python 3.12+ / FastAPI** (auto OpenAPI at `/docs`) |
| ORM / migrations | SQLAlchemy 2.x + Alembic (batch mode for SQLite) |
| DB | SQLite with WAL, `foreign_keys=ON`, `busy_timeout=5000`. Backups via `sqlite3 .backup` cron or Litestream → R2 |
| Auth | Username + password (argon2id). JWT access token (15 min) plus a rotating refresh token (30 days) stored hashed in the DB |
| OCR | **RapidOCR** (PaddleOCR PP-OCR models on ONNX Runtime, EN+ZH) plus a heuristic field extractor |
| Excel | `openpyxl` |
| PDF render | `pypdfium2` (page-1 thumbnail, OCR input) |
| Images | Pillow + `pillow-heif` (HEIC → JPEG) |
| Storage | `StorageBackend` interface: `R2Storage` (boto3 S3 API) primary, `LocalStorage` fallback |
| Scheduler | In-process asyncio loop (every 5 min, plus on startup), idempotent jobs |
| Runtime | `uvicorn` with 1 worker behind Caddy (auto-TLS); systemd or Docker. Expected RSS ≈ 400–600 MB with OCR loaded |

---

## 5. Glossary

- **User**: a person. It is either **registered** (can log in) or a **placeholder** (display name only, created by someone else, claimable).
- **Super admin**: the first account, seeded on first boot. Manages users (disable, reset password). It has no special access to group data.
- **Group**: people who share bills. Every expense belongs to exactly one group. Each group has a fixed currency and a reusable **join code**.
- **Default group**: each user's undeletable "only-me" group. Users may create additional only-me groups.
- **Expense type**: a per-user category (e.g. "Groceries"). It points to one routing.
- **Routing**: a per-user named channel (e.g. "Home"). It maps to one group. Several types can share a routing, so changing one mapping redirects all of them.
- **Auto-rule**: a per-user rule that matches a store name and sets the type and/or group.
- **Period**: a time window within a group. Recurring groups roll periods automatically. Standard groups have a single never-ending period.
- **Expense (bill)**: paid by one payer and split among participants.
- **Share**: one participant's portion of an expense. It can be **checked** (paid off).
- **Guest**: a participant on a bill who is not a member of the group.
- **Settlement**: a recorded payment "A paid B $X" within a group period.

---

## 6. Feature Design

### 6.1 Accounts, Join Codes, Placeholders (F1)

**Bootstrap**: on first start, if no users exist, create a super admin from `ADMIN_USERNAME` / `ADMIN_PASSWORD` env vars (or `python -m app.cli create-admin`).

**Group join codes (the only way in)**:
- Every shareable group has a **join code** (e.g. `LIVE-7K2Q9M`). Owners and admins can view, regenerate, or disable it. It has no expiry and is not single use. Regenerating invalidates the old code.
- **New person**: registers with `join_code + username + password + display_name` (email optional and unused). The account is created and joins that group as a `member`.
- **Existing user**: enters a join code while logged in and joins the group.
- The super admin can also create accounts directly (for bootstrap or recovery).
- Forgotten password: there is no email, so the super admin resets it and gets a temporary password that must be changed on next login.
- Default groups (personal) have no join code.

**Placeholders**:
- Any group member can create a placeholder (display name only). This can happen inline while adding a bill participant (guest) or from a group's member list.
- Placeholders cannot log in. Their shares can be checked by the payer (see §6.7).

**Claiming a placeholder (via the join code flow)**:
- When someone joins with a group code (at registration or as an existing user), the response lists the group's **unclaimed placeholders**, including people who only appeared as guests on that group's bills. The joiner can pick "I am Bob" or skip.
  - **New registration + claim** = **convert**: the placeholder row becomes the registered user (same `user_id`, so nothing moves).
  - **Existing user + claim** = **merge**: in one transaction, every reference (memberships, shares, expenses paid/created, settlements, attachments uploaded, period snapshots) is re-pointed to the existing user. If both had shares on the same bill, they are combined into one share. The placeholder is tombstoned (`merged_into_id`).
- A claim is recorded in `audit_log`. It cannot be undone through the API.
- An existing user can also claim a placeholder later from the group's member list (`POST /groups/{id}/placeholders/{pid}/claim`).

### 6.2 Groups (F4)

- Fields: `name`, `description`, `currency` (ISO 4217, fixed after the first expense), `is_recurring`, cadence settings (§6.6), `owner_id`, `archived_at`.
- **Roles**: `owner` (creator; may transfer ownership), `admin`, `member`.
  - Owner and admins: add or remove members, promote or demote admins, edit settings, close a period early, archive the group.
  - Members: add bills and settlements, and view everything in the group.
- Members can be registered users or placeholders.
- Removing a member is a soft removal (`left_at`). Their historical shares remain.
- **Only-me groups**: any group with a single member. The **default group** (`is_default=1`) is auto-created at registration or claim. It cannot be deleted, archived, or given other members. Users can create more only-me groups (e.g. "Work reimbursable").

### 6.3 Expense Types & Routings (F3)

All of these are **per-user**. Each user sees and manages only their own.

```
alice: Groceries ─┐
       Cleaning  ─┴─► routing "Home" ──► group "Livexpense"
       Lunch     ───► routing "Me"   ──► group "Personal" (default)
bob:   Food      ───► routing "Home" ──► group "Livexpense"
```

- `routing.group_id` is nullable. An unmapped routing resolves to the user's default group.
- **Per-group default type** (`user_group_settings`): each user can pick one of their types as the default for a group. Choosing the group by hand pre-selects that type. This is the reverse direction of type → routing → group.
- **Seed for every new user** (registration or placeholder convert): only the protected **Personal** group (`is_default`, undeletable), plus a routing and a type named "Personal" pointing to it. Users create any other groups, routings, and types themselves.
- Deleting a routing that types still use is blocked. Types and routings are archived rather than hard-deleted once they are used by an expense.
- An expense stores `expense_type_id` (the creator's type). Other members viewing the bill see the creator's type name (read-only label).

### 6.4 Auto-Routing Rules (F3)

- A per-user, ordered list. Each rule has:
  - **match**: field `merchant` (store name) with mode `contains` | `equals` | `regex`, case-insensitive. Optionally `title` too.
  - **action**: `set_type_id` and/or `set_group_id` (at least one).
- **Resolution** (`POST /expenses/resolve`, also used by the OCR response):
  1. The first matching rule (by `priority`) supplies the type and/or group.
  2. If the rule set a type but no group, use type → routing → group.
  3. If the user picked a type manually, use type → routing → group.
  4. Fallback: the default group.
  5. The user can always override the result before saving. The backend validates that the caller is a member of the target group.

### 6.5 Expenses & Splitting (F4)

- Fields: `title`, `merchant`, `amount` (integer minor units, > 0), `expense_date`, `expense_type_id`, `group_id`, `period_id` (derived), `payer_id` (**single payer**), `split_method`, `notes`, `ocr_result` (JSON), audit columns, and soft delete. Currency comes from the group.
- **Participants** default to all current group members. The client can:
  - exclude members for this bill, and
  - add **guests**: existing users or new placeholders, for **this bill only**. Group membership is unchanged. A guest's debt shows in that group period's balances, marked as a guest.
- The payer does not need to be a participant (e.g. someone paid on behalf of others).
- **Split methods**:
  - `equal`: everyone gets `amount / n`.
  - `exact`: per-participant amounts. They must sum to `amount`.
  - `percent`: per-participant percent with 2 decimals. Must sum to 100.
  - `shares`: per-participant integer or decimal weights.
  - `itemized`: a list of **line items** `{name, amount, participant_ids[]}`. Each item is split equally among its participants. The remainder (`amount − Σ items`, i.e. tax/tip/fees) is allocated **proportionally to each person's item subtotal**. Items with no participants are split among all bill participants.
- **Rounding**: all math is done in integer cents. Leftover cents go to participants one at a time in descending order of fractional remainder, tie-broken by participant id (deterministic). Invariant: `Σ shares == amount`.
- **Edit/delete permission**: the bill's **creator, payer, or group owner** (admins are not included). Not allowed once the period is frozen.
- Every create, update, delete, and check action is written to `audit_log`.

### 6.6 Recurring Groups & Periods (F5)

Every group has periods. A standard group has one open period with no end. A recurring group rolls periods.

- Settings: `cadence` (`monthly` default | `weekly` | `every_n_days`), `anchor` (e.g. day 1 of month), `timezone` (e.g. `America/Toronto`), and `grace_days` (default **3**).
- **Period states**: `open` → `grace` → `frozen`.

```
           cutoff (Oct 1 00:00)          grace ends (Oct 4 00:00)
Sep period ─── open ────────┤── grace ───────┤── frozen (snapshot taken) ──►
Oct period                  ├── open ─────────────────────────────────────►
```

- **Placing a bill**: the period is chosen by `expense_date`. During grace, a bill dated Sep 28 goes to the Sep period, which is still editable. A bill dated Oct 2 goes to Oct. Once Sep is frozen, a bill dated in Sep is **rejected** with a hint to date it inside the current open period.
- **When grace ends** (scheduler job):
  1. Snapshot the members (`period_members`, including guests who appeared on bills).
  2. Snapshot the closing balances: `period_balances` (net per person) and `period_transfers` (simplified "who pays whom").
  3. Set `status = frozen`. Amounts, participants, payer, and dates become immutable.
- **The next period starts from zero.** Debts never roll forward; they stay attached to the period they came from.
- **After freeze**, you can still check off shares, record settlements against that period, and add attachments. The period view shows "outstanding vs settled" live against the frozen snapshot.
- The owner or an admin can **cut now**: end the current period early. The next period starts immediately, and the old period enters grace.
- **Unfreeze** is not supported. This keeps snapshots trustworthy.
- Changing the cadence or grace setting applies from the next period on.
- The scheduler runs at startup and every 5 minutes and is idempotent, so downtime catches up correctly.

### 6.7 Checking Shares & Settlements (F6)

- **Share check**: `expense_shares.checked_at / checked_by_id`. Allowed for the **payer (creditor) or the debtor**. For placeholder debtors, the payer can check. Can be unchecked by the same parties.
- **Settlement**: "A paid B $X" in a group period with an optional note and attachments (e.g. an e-transfer screenshot). It can **link shares** it covers. Linking auto-checks those shares (`expense_shares.settlement_id`).
- **Balance math** (no double counting):
  `owed(A→B) = Σ unchecked shares where A is the debtor and B the payer − Σ settlements A→B that link no shares`
  Settlements that link shares are already reflected by the checked flags.
- A bill is **fully checked** when every non-payer share is checked.

### 6.8 Balances & Simplification

- Per group period: net per person, and a pairwise owed matrix.
- **Simplified transfers**: a greedy min-cash-flow on net balances (largest debtor pays the largest creditor), giving at most n−1 transfers. These are suggestions only.
- A cross-group "my overview" sums what I owe and am owed across all open and unsettled periods, per currency.

### 6.9 OCR (F2)

| Engine | RAM | Phone photos | Role |
|---|---|---|---|
| **RapidOCR** (ONNX, PP-OCR EN+ZH) | ~250–400 MB | Good | **Primary** |
| Tesseract 5 | ~100 MB | Weak | Not used (option if RAM is ever tight) |

- **Endpoint**: `POST /ocr/receipt` with an image or PDF upload. It is synchronous with a concurrency gate. All limits come from config (env), not hard-coded:
  - `OCR_MAX_CONCURRENCY` (default `1`): jobs running at once.
  - `OCR_MAX_QUEUE` (default `3`): callers waiting. Beyond that the endpoint returns `429 ocr_busy`.
  - `OCR_TIMEOUT_SECONDS` (default `30`).
  - `OCR_ENABLED` (default `true`).
- **Pipeline**:
  1. Decode. For PDFs, render page 1 at 200 DPI. Fix EXIF rotation, convert HEIC, and downscale to 1600 px on the long edge.
  2. Run RapidOCR to get lines with bounding boxes and confidence.
  3. Extract fields:
     - **total**: lines matching `TOTAL|GRAND TOTAL|AMOUNT DUE|BALANCE DUE|合计|总计|總計|实付|應付`, excluding `SUB|TAX|GST|HST|PST|CHANGE|TIP|CASH|找零`. Take the money amount on the same line or the next one. Prefer the bottom-most match. Fallback: the largest amount in the lower 50%.
     - **merchant**: the highest-confidence line among the top 3 that is not an address, phone number, URL, or date.
     - **date**: regex for `YYYY-MM-DD`, `MM/DD/YY(YY)`, `DD/MM/YYYY`, and `2026年10月5日`.
  4. Return `{merchant, total, date, confidence: {merchant, total, date}, raw_lines, suggestion: <resolve result>}`.
- The model is lazy-loaded on the first call and stays resident. `OCR_ENABLED=false` disables it (the endpoint returns `503`).
- The uploaded file can be kept as an attachment (an optional `keep=true` returns an `attachment_id` to link when saving the expense).

### 6.10 Attachments & Storage (F7)

- **Allowed**: JPEG, PNG, WebP, HEIC/HEIF (converted to JPEG on upload), and PDF. Detected by **magic bytes**. **Max 10 MB** per file (`UPLOAD_MAX_MB`). There is no limit on attachment count.
- They attach to **expenses** and **settlements**. Upload flow: `POST /attachments` returns `attachment_id` (unlinked). Linking happens via expense or settlement create/update, or `POST /expenses/{id}/attachments`. Unlinked uploads are simply left in place (no cleanup job; it's an internal tool).
- A **thumbnail** (JPEG, max 400 px) is generated for images and page 1 of PDFs. Re-encoding strips EXIF and GPS data.
- **Storage policy: R2 primary, local fallback**:
  - If R2 is configured, upload to R2. If that fails, write locally with `sync_status = pending`. A scheduler job retries the upload to R2 and **deletes the local copy after success**.
  - If R2 is not configured, store locally (`DATA_DIR/files/...`).
  - Reads go through an authenticated endpoint. For R2 objects it returns a 302 redirect to a short-lived presigned URL. Local files are streamed directly.
- Keys: `attachments/{yyyy}/{mm}/{uuid}.{ext}` and `thumbs/...`. A `sha256` is stored for integrity.

### 6.11 Excel Export (F8)

- `POST /exports` with `{from, to, group_ids?, include_settlements: true}` returns a **ZIP** download (built synchronously; the data is small).
- **Scope**: bills where the caller is the **payer or a participant**, within groups the caller belongs to (including guest appearances).
- **ZIP layout**:
  ```
  billsplit-export-2026-09-01_2026-09-30.zip
    expenses.xlsx
    attachments/
      2026-09-28_Costco_<shortid>.jpg
      2026-09-15_Hydro_<shortid>.pdf
  ```
- **Workbook**:
  - **Summary** sheet: export parameters. Per group and period: currency, total spent by the group, my paid, my share, net, settled, and outstanding. Per-currency subtotals (no mixing currencies).
  - **Expenses** sheet columns: Date | Group | Period | Type | Title | Merchant | Payer | Total | My Share | Split | Participants | Checked | Notes | Invoice (thumbnail) | Files (link).
    - Bold, colored header; frozen header row and first column; auto-filter; currency number format per group; column widths fitted to content (capped); wrapped text for participants and notes; zebra striping; thumbnail embedded in the Invoice cell (row height about 80 px); relative hyperlinks into `attachments/` that work offline after unzipping.
  - **Settlements** sheet: Date | Group | Period | From | To | Amount | Note | Files.
  - **Line Items** sheet (itemized bills only).

---

## 7. Data Model (SQLite)

Money is stored as `INTEGER` minor units. IDs are `TEXT` UUIDv7. Timestamps are ISO-8601 UTC `TEXT`. Dates are `YYYY-MM-DD`.

```sql
users(id PK, username UNIQUE NULL, display_name, email NULL, password_hash NULL,
      is_placeholder BOOL, is_super_admin BOOL, must_change_password BOOL,
      disabled_at NULL, created_by_id FK NULL, merged_into_id FK NULL, created_at, updated_at)
refresh_tokens(id PK, user_id FK, token_hash, expires_at, revoked_at NULL, created_at)
groups(id PK, name, description, currency, owner_id FK, is_default BOOL,
       join_code UNIQUE NULL, join_enabled BOOL,
       is_recurring BOOL, cadence NULL, cadence_n NULL, anchor_day NULL, timezone, grace_days,
       archived_at NULL, created_at, updated_at)
group_members(group_id FK, user_id FK, role CHECK(role IN ('owner','admin','member')),
              joined_at, left_at NULL, PK(group_id,user_id))
periods(id PK, group_id FK, starts_on, ends_on NULL, grace_until NULL,
        status CHECK(status IN ('open','grace','frozen')), frozen_at NULL)
period_members(period_id FK, user_id FK, is_guest BOOL, PK(period_id,user_id))
period_balances(period_id FK, user_id FK, paid, owed, net, PK(period_id,user_id))
period_transfers(id PK, period_id FK, from_user_id, to_user_id, amount)

routings(id PK, user_id FK, name, group_id FK NULL, archived_at NULL, UNIQUE(user_id,name))
expense_types(id PK, user_id FK, name, routing_id FK, icon NULL, color NULL, archived_at NULL,
              UNIQUE(user_id,name))
user_group_settings(user_id FK, group_id FK, default_expense_type_id FK NULL, PK(user_id,group_id))
user_preferences(user_id PK FK, group_order JSON, group_order_revision INTEGER)   -- personal group order; first = preferred group
auto_rules(id PK, user_id FK, priority, field CHECK(field IN ('merchant','title')),
           mode CHECK(mode IN ('contains','equals','regex')), pattern,
           set_type_id FK NULL, set_group_id FK NULL, enabled BOOL)

expenses(id PK, group_id FK, period_id FK, expense_type_id FK NULL, title, merchant NULL,
         amount INTEGER, expense_date, payer_id FK, split_method, notes NULL, ocr_result JSON NULL,
         created_by_id FK, created_at, updated_at, deleted_at NULL)
expense_shares(expense_id FK, user_id FK, is_guest BOOL, split_value NULL, amount INTEGER,
               checked_at NULL, checked_by_id NULL, settlement_id FK NULL, PK(expense_id,user_id))
expense_items(id PK, expense_id FK, name, amount INTEGER, position)
expense_item_participants(item_id FK, user_id FK, PK(item_id,user_id))

settlements(id PK, group_id FK, period_id FK, from_user_id, to_user_id, amount, settled_on,
            note NULL, created_by_id, created_at, deleted_at NULL)

attachments(id PK, owner_type NULL CHECK(owner_type IN ('expense','settlement')), owner_id NULL,
            original_name, mime, size, sha256, storage_backend CHECK(storage_backend IN ('r2','local')),
            storage_key, thumb_key NULL, sync_status CHECK(sync_status IN ('ok','pending')),
            uploaded_by_id, created_at, deleted_at NULL)

audit_log(id PK, actor_id, entity, entity_id, action, diff JSON, created_at)
```

**Invariants** (service layer + tests):
- `Σ expense_shares.amount == expenses.amount`. For itemized bills, `Σ expense_items.amount ≤ expenses.amount`.
- Exactly one `is_default` group per user. It has a single member and is never deleted.
- No bill or settlement writes (other than check, link, or attach) in `frozen` periods.
- An expense's `period_id` must be consistent with its `expense_date` and the group's periods.

---

## 8. Project Layout

```
BillSplit/
  app/
    main.py  config.py  db.py  security.py  cli.py
    models/       # SQLAlchemy models
    schemas/      # Pydantic I/O
    api/          # auth, admin, users, groups (join/claim), periods, routings,
                  # expense_types, auto_rules, expenses, settlements, attachments, ocr, exports, me
    services/     # splitting, balances, periods, resolve, claims/merge, export, ocr, audit
    storage/      # base.py, local.py, r2.py
    jobs/         # scheduler: period rollover/freeze, attachment sync, unlinked GC
  migrations/  tests/  docs/
  pyproject.toml  Dockerfile  .env.example
```

---

## 9. Security

- Argon2id; login rate limit (5/min per username+IP, in memory); JWT HS256 with a secret from env; refresh-token rotation with reuse detection.
- Authorization is centralized: `require_member(group)`, `require_group_admin(group)`, `can_edit_expense`, `can_check_share`.
- Attachments are only served to members of the owning group (and to guests on that bill).
- Upload hardening: magic-byte check, size cap, Pillow decompression-bomb limit, PDFs served with `Content-Disposition: attachment` and `X-Content-Type-Options: nosniff`.

---

## 10. Decisions Log

| # | Question | Decision |
|---|---|---|
| 1 | Starting point | Build from scratch (FastAPI + SQLite) |
| 2 | Recurring semantics | Rolling periods; next period starts at 0; history kept |
| 3 | Late bills | Grace window, date-based, default 3 days |
| 4 | Settle/check in frozen period | Allowed |
| 5 | Types/routings scope | Per-user |
| 6 | Default group | Undeletable only-me group; more only-me groups allowed |
| 7 | Auto-routing rules | By store name; can set type and/or group |
| 8 | Payers | Single payer |
| 9 | Split methods | Equal, exact, percent, shares, itemized |
| 10 | Outsiders on a bill | Bill-only guests |
| 11 | Registration | Group join code (reusable, regenerable); seeded super admin |
| 12 | Placeholder claim | Picked during join-code flow; convert (new) or merge (existing) |
| 13 | Edit/delete bill | Creator, payer, group owner |
| 14 | Currency | One per group, no FX |
| 15 | Storage | R2 primary, local fallback (with re-sync) |
| 16 | Export | Bills I'm in; ZIP of xlsx + attachments |
| 17 | Check permission | Payer or debtor |
| 18 | OCR | RapidOCR, EN+ZH, 2 GB instance |
| 19 | Uploads | 10 MB per file, HEIC accepted (converted to JPEG), no count limit, no orphan cleanup |
| 20 | Group admin | Owner + admins |
| 21 | Seed data | Only the protected Personal group + a "Personal" routing and type. (Life Expense / Reimbursement were seeded until 0.2.1 and removed; the unused copies were deleted from the test DB) |
| 22 | OCR limits | Configurable via env (defaults 1 running / 3 queued / 30 s) |
| 23 | R2 re-sync | Delete the local copy after a successful sync |
| 24 | Frozen-period bills | Rejected (no unfreeze) |
| 25 | Admins editing others' bills | Not allowed (creator, payer, owner only) |
| 26 | Per-group default type | Each user can set a default expense type per group; picking the group pre-selects it (still overridable). The seeded Personal group defaults to the "Personal" type |
| 27 | Auto-rule results | Always a suggestion; the user can override type and group before saving |
| 28 | Export of several groups | One workbook: a single Expenses sheet with a Group column + per-group/period Summary |
| 29 | Bills dated before a group's first period | Go into the first period. Bills dated after the open period are rejected |
| 30 | Deployment network | Backend reachable only inside the Tailscale tailnet (`tailscale serve`) |
| 31 | Personal group order | Each user orders their available groups; the first is the preferred group for new expenses (resolve/OCR fallback). Separate from the protected default (private) group. Optimistic concurrency via a per-user revision |
