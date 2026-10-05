# BillSplit — Frontend Handout

> For the frontend agent. Read this first, then use [`api.md`](api.md) as the contract.
> Background (optional): [`design.md`](design.md). Backend OpenAPI: `GET /api/v1/openapi.json`. Swagger UI: `/docs`.

---

## 0. Connecting to the backend

The backend and frontend run on the same **Tailscale** network (tailnet). The backend is published inside the tailnet with `tailscale serve`, so it gets HTTPS and a stable MagicDNS name. It is not reachable from the public internet.

**Current test backend (running now):** `http://nuc.tailcfa4ec.ts.net:8000/api/v1`. Swagger: `http://nuc.tailcfa4ec.ts.net:8000/docs`. Test login: `admin` / `password`. It's plain HTTP inside the tailnet (WireGuard-encrypted) and runs on the owner's NUC, started manually, so it may be offline. Check `GET /api/v1/health`. The production values below apply once it moves to its final server.

| Item | Value |
|---|---|
| API base URL | `https://<backend-host>.<tailnet>.ts.net/api/v1` (ask the owner for the exact host; put it in an env var such as `VITE_API_BASE_URL`, never hard-code it) |
| Swagger UI | `https://<backend-host>.<tailnet>.ts.net/docs` (try every endpoint in the browser) |
| OpenAPI JSON | `https://<backend-host>.<tailnet>.ts.net/api/v1/openapi.json` (generate a typed client from it if you like, e.g. `openapi-typescript`) |
| Health check | `GET /api/v1/health` (no auth) |
| Local dev backend | `http://localhost:8000/api/v1` (see the README: `uvicorn app.main:app --reload`) |

- **CORS**: the backend allows the origins in its `CORS_ORIGINS` setting. During development it is `["*"]`. For production, tell the owner your frontend origin (e.g. `https://<frontend-host>.<tailnet>.ts.net`). Auth uses a Bearer header, not cookies, so no credentials mode is needed.
- **Every device that opens the app must be on the tailnet** (phones too, via the Tailscale app), because the browser calls the API directly. The alternative is a frontend server that proxies `/api` to the backend; then only that server needs to reach the API, but the users still reach the frontend over the tailnet.
- HTTPS from `tailscale serve` also makes PWA install and camera access work on phones.

---

## 1. What we're building

An **internal** expense-tracking and bill-splitting app for a small set of people (roommates, family, a few coworkers). It replaces a Splitwise workflow where the users created a **new group every month**. Here one group rolls over automatically each month.

- **No real payments.** The app only records who paid what, who owes whom, and lets people tick things off.
- **Low traffic, trusted users.** Favor clarity and speed of data entry over defensive UX.
- **Mobile-first.** The main action is "snap a receipt → confirm → save" on a phone. Desktop is used for reviewing and exporting.
- The stack is your choice. It must work as a responsive web app (PWA is a plus, so the camera works from the home screen).

---

## 2. Concepts the UI must make obvious

| Concept | What the user should understand |
|---|---|
| **Group** | A set of people sharing bills. Every bill lives in exactly one group. Each group has one currency. |
| **Default group ("Personal")** | Just me. It can't be deleted. My private expenses go here. |
| **Only-me groups** | Any group with just me. New users start with just **Personal** and add other groups themselves. |
| **Expense type** | My own categories (e.g. "Groceries"). Each type points to a **routing**. Each group can also have **my default type** (`group.default_expense_type_id`): picking that group pre-selects the type. Types don't drive much yet beyond labeling and routing. |
| **Routing** | My own "where does this go" switch. Example: routing "Life Expense" → group "Livexpense". Changing the routing's group moves all its types at once. |
| **Auto-rule** | "If the store name contains COSTCO → type Groceries (and/or group X)". Saves the user picking anything. |
| **Recurring group & periods** | A group can cut off every month (or week). Each month is a **period**. At month end there is a **grace window** (default 3 days) where last month's bills can still be added or edited. Then the period is **frozen**: amounts are locked, a snapshot of who owes whom is saved, and the new month starts at **$0**. Old months stay browsable and payable. |
| **Guest** | Someone added to a single bill who is not in the group. |
| **Placeholder** | A person without an account (just a name). They can be split with like anyone else. Later they join with the group's code and "claim" themselves, and all their history becomes theirs. |
| **Checked share** | "This person has paid me back for this bill." Either the payer or the debtor can tick it. |
| **Settlement** | "Bob paid Alice $60." It can be linked to specific shares (auto-ticks them) or be a lump sum. |
| **Join code** | Each shareable group has a code like `LIVE-7K2Q9M`. It's the **only** way to sign up. |

**Ownership of types/routings/rules is per user.** Two users in the same group have different type lists. On a bill created by someone else, show their type name as a plain label. Don't offer it in pickers.

---

## 3. Screens

### 3.1 Auth
- **Login**: username + password.
- **Join / Sign up** (`/join?code=...` deep link supported):
  1. Enter the code → `GET /auth/join-preview` → show "You're joining **Livexpense**".
  2. If placeholders exist: "Are you one of these people?" Show a list of names plus "No, I'm new". Explain that picking a name brings their existing bills to the account.
  3. Username, display name, password → `POST /auth/register`.
- **Forced password change** screen when `me.must_change_password` (after an admin reset).
- Logged-in users can **join another group** with a code (`POST /groups/join`), with the same placeholder choice. There, the choice means **merge** into my account.

### 3.2 Home / Overview
- `GET /me/overview`: big numbers per currency (**I owe / Owed to me / Net**).
- A list of groups with outstanding amounts per period (e.g. "Livexpense · Sep (frozen) · you are owed $91"). Tapping one goes to that period.
- A prominent **＋ Add bill** button (camera-first), visible everywhere.

### 3.3 Add / Edit bill (most important screen)
Order of fields, optimized for speed:
1. **Receipt** (optional): camera or file. Upload via `POST /ocr/receipt?keep=true` → pre-fill merchant, total, date, type, and group. Show a subtle "auto-filled, please check" hint and low-confidence fields highlighted (confidence < 0.6). OCR may fail or be busy (`429`). Then just keep the attachment (upload via `POST /attachments`) and let the user type.
2. **Amount** (big input) and **Title** / **Merchant**.
3. **Type** (my types). Changing the type or merchant calls `POST /expenses/resolve` and **auto-selects the group** unless the user already picked a group manually. Example: a rule "HEB → Life Expense" makes Life Expense the *default choice*, and the user can still pick something else.
4. **Group**. Defaults from resolve. Shows the currency. When the user picks a group by hand and hasn't chosen a type, call resolve with `group_id` (or read `group.default_expense_type_id`) to pre-select that group's default type.
5. **Date**. Defaults to today. If the group is recurring and the date falls in a frozen period, show the error inline (`409 period_frozen`). Tell the user to date it in the current period instead.
6. **Paid by**. Defaults to me. Picker from group members + current participants.
7. **Split with**: a checklist pre-filled with **all current group members** (from `GET /groups/{id}/members`). The user can untick people, **＋ Add someone** (search `GET /people`), or create a new person by name (`POST /placeholders`, no `group_id`, so they become a guest). Mark guests visibly.
8. **Split method** tabs: Equal · Exact · % · Shares · Itemized.
   - Show live per-person amounts via `POST /expenses/preview-split` (debounce about 300 ms).
   - Exact/%: show a "remaining" counter, and block save until it reaches 0 / 100%.
   - **Itemized**: a list of line items (name, amount, who). Show "Tax / tip / fees: $X (split proportionally)" computed as total − Σ items. Items with nobody selected are split among everyone.
9. **Attachments**: images or PDFs, any count, 10 MB each (`POST /attachments`, then `attachment_ids`). HEIC from iPhone is fine.
10. **Notes**.

Edit uses the same screen (`PATCH`). Respect `expense.permissions.can_edit`. If the period is frozen, show the bill read-only, except **ticking shares and adding attachments**.

### 3.4 Group page
- Header: name, currency, recurring badge ("Monthly · cuts off on the 1st · 3-day grace"), and the **current period chip** (`open` / `grace` / `frozen`).
- **Period switcher**: Current, then past periods (`GET /groups/{id}/periods`).
- Tabs for the selected period:
  - **Bills** (`GET /expenses?period_id=`): date, title, payer, total, **my share**, a checked state icon, and an attachment icon.
  - **Balances** (`GET /periods/{id}/balances`): per-member net, "who pays whom" (`suggested_transfers`), and a **Record payment** button pre-filled from a transfer. For frozen periods, also show the snapshot and a progress bar (`progress.outstanding_now / total_owed_at_freeze`).
  - **Payments** (`GET /settlements?period_id=`).
- **Members** (owner/admin can manage): roles, add an existing person or a placeholder by name, remove, the **join code** with copy/share/regenerate and an on/off toggle, and placeholders with a "this is me" claim button.
- **Settings** (owner/admin): name, currency (locked after the first bill), recurrence (cadence, anchor, timezone, grace days; takes effect next period), and **Cut period now** (with confirmation).
- The default group shows none of the sharing features.

### 3.5 Bill detail
- All fields, attachments (thumbnails → full view; PDFs open or download), and per-person shares with **checkboxes**. A checkbox is enabled if I'm the payer or that debtor (the backend enforces this too). Payer-only state for placeholder debtors.
- "Fully checked" badge.

### 3.6 Record payment (settlement)
- From, To, Amount, Date, Note, attachments (e.g. a screenshot of an e-transfer).
- Optional "Which bills does this cover?": a list of unchecked shares from `from` to `to` in that period (`GET /expenses?period_id=..&checked=false`). Picking them fills the amount automatically.
- Works for frozen periods too.

### 3.7 My setup (Types, Routings, Rules)
- **Routings**: name → group dropdown. This is the main control for "where does Life Expense go now?"
- **Types**: name, icon, color, routing.
- **Per-group default type**: on each group's settings (for me), choose a default type (`PUT /groups/{id}/my-settings`).
- **Auto-rules**: an ordered, drag-to-reorder list (`PUT /auto-rules/order`). The editor has: field (store name/title), mode (contains/equals/regex), pattern, and set type and/or set group. Include a **"Test"** box calling `POST /auto-rules/test`.

### 3.8 Export
- Date range (presets: this month, last month, custom), group multi-select, and an "include payments" toggle.
- `POST /exports` → download the ZIP (blob). Explain: "Unzip and open expenses.xlsx. Invoice links open the files in the attachments folder."

### 3.9 Admin (super admin only, `me.is_super_admin`)
- A user list, create user, reset password (show the temporary password once), and disable/enable.

---

## 3.10 Group order and the default group for new bills

- The order of groups in the picker (and in the group list) is **per user and stored on the server**: `GET /me/group-order` (`GET /groups` already returns this order).
- **The first group is the default group for a new bill** (`preferred_group_id`, also on `GET /me`). This is *not* `me.default_group_id`, which is the protected private ledger. Keep both concepts separate in the UI.
- Drag to reorder → `PUT /me/group-order` with the full list and the `revision` you read → replace your state with the response.
- On `409 group_order_conflict`: show `error.details.current`, tell the user the order changed elsewhere, and let them drag again. Don't silently re-PUT.
- Local order saved in the browser before this existed: **don't push it automatically.** Use the server order. If the user drags, that PUT becomes the server's order. You can now remove the "order is saved on this device only" notice.
- New-bill form: pre-select `preferred_group_id`, then call `POST /expenses/resolve`. Without `group_id`, it returns the preferred group plus my default type for it, unless a merchant rule matches. When the user changes the group by hand, clear the type and call resolve with only `group_id` to get that group's default type. When the user then picks a type by hand, keep the group they chose.
- `POST /expenses` still needs the final `group_id`. Changing the order never moves existing bills.
- Group balances: keep using `GET /periods/{id}/balances` → `pairwise` (every remaining who-owes-whom pair in the whole period, not simplified) and `POST /settlements`.

## 4. Rules & Gotchas

1. **Money is integer minor units.** Never use floats. Parse user input "12.30" → `1230` using the currency's `minor_units` (`GET /meta/currencies`; JPY = 0). Display with `Intl.NumberFormat`.
2. **Dates**: send `YYYY-MM-DD` for bill and settlement dates (the user's local date). Don't send timestamps.
3. **Auth**: store the access token in memory and the refresh token in persistent storage. On `401 unauthorized`, refresh once and retry. Refresh rotates the token, so save the new one. Serialize concurrent refreshes (a single in-flight promise).
4. **Files** need the Bearer header. Fetch thumbnails as blobs → `URL.createObjectURL`. Allow redirects (R2 presigned URLs).
5. **Errors**: switch on `error.code`, not the message. Show `details.fields` inline for `validation_error`.
6. **Permissions**: the backend is the source of truth. Use `expense.permissions`, `group.my_role`, and `me.is_super_admin` to hide controls. Still handle `403`.
7. **Group currency**: never mix currencies in one total. Overview totals are per currency.
8. **Participants vs members**: the participant list on a bill can include guests and exclude members. Always render from `expense.shares`, not from the group member list.
9. **OCR is best-effort.** Never auto-save from OCR. It's a pre-fill only.
10. **Resolve-on-change** must not clobber a group the user picked by hand. Track a "group manually set" flag.
11. **Frozen periods**: disable editing amount, payer, participants, and date. Keep ticking checks, adding attachments, and recording payments enabled.
12. Lists use cursor pagination (`next_cursor`). Use infinite scroll.

---

## 5. Suggested build order
1. Auth (login, join with placeholder claim, refresh) and the app shell.
2. Groups list, group page (bills + balances for the current period), members, join code.
3. Add/Edit bill with all split methods + preview-split, without OCR first.
4. Attachments, then OCR pre-fill.
5. Period switcher, frozen states, settlements, check-off.
6. Types, routings, and rules setup; resolve-on-change.
7. Overview, export, admin.

## 6. Out of scope / don't build
- Payment integrations, email or notifications, public sign-up, currency conversion, and editing frozen periods.

## 7. Questions?
If the API is missing something you need, don't fake it client-side. Write it down in a `FRONTEND_REQUESTS.md` with the screen, the data needed, and a proposed endpoint, so the backend can add it.
