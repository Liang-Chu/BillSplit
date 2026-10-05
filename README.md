# BillSplit

Internal expense management and bill-splitting backend: FastAPI, SQLite, RapidOCR, and openpyxl.

## Docs
- [Frontend handout](docs/frontend-handout.md): start here if you're building the UI
- [API design](docs/api.md): REST contract (the live OpenAPI schema at `/api/v1/openapi.json` is authoritative)
- [Design document](docs/design.md): architecture, data model, decisions
- [Operations (Chinese)](docs/operations.md): starting/stopping the test server, logs, troubleshooting

## Run locally

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env            # set JWT_SECRET, ADMIN_USERNAME, ADMIN_PASSWORD
.venv/bin/uvicorn app.main:app --reload --port 8000
```

- Swagger UI: http://localhost:8000/docs
- Health: http://localhost:8000/api/v1/health
- On first start, migrations run automatically and the super admin from `.env` is created (only if there are no users yet).
- Alternative: `python -m app.cli create-admin <username> <password> [display name]`.

Tests: `.venv/bin/pytest`

## Start / stop (test server)

Full guide (Chinese): [docs/operations.md](docs/operations.md)

```bash
HOST=$(tailscale ip -4) scripts/start.sh   # start in background, reachable from the tailnet
scripts/stop.sh                            # stop
tail -f data/server.log                    # logs
```

Without `HOST`, it listens on `127.0.0.1` only.

## Deploy (small EC2 + Tailscale)

Target: 2 GB RAM (e.g. `t4g.small`). The process uses about 360 MB with the OCR model loaded.

1. Install Tailscale on the server and join the tailnet: `curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up`.
2. Run the API bound to **localhost only** (`--host 127.0.0.1 --port 8000`) via systemd, or via Docker with `ports: ["127.0.0.1:8000:8000"]`. Keep port 8000 closed in the EC2 security group.
3. Publish it inside the tailnet with HTTPS: `sudo tailscale serve --bg 8000`.
   The API is now at `https://<host>.<tailnet>.ts.net/api/v1` for every device on the tailnet, and is not reachable from the internet.
4. Set `CORS_ORIGINS=["https://<frontend-host>.<tailnet>.ts.net"]` in `.env` once the frontend's URL is known.

Example systemd unit (`/etc/systemd/system/billsplit.service`):

```ini
[Unit]
Description=BillSplit API
After=network-online.target

[Service]
User=billsplit
WorkingDirectory=/opt/billsplit
EnvironmentFile=/opt/billsplit/.env
ExecStart=/opt/billsplit/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
Restart=always

[Install]
WantedBy=multi-user.target
```

**Keep a single worker.** The scheduler and OCR queue live in-process, and SQLite prefers one writer.

### Backups
Everything lives in `DATA_DIR` (`billsplit.db` plus `files/`). For a consistent copy, use `sqlite3 data/billsplit.db ".backup backup.db"`, or run Litestream to R2.

### Cloudflare R2
Set the four `R2_*` variables. New uploads go to R2. If R2 is unreachable, files are stored locally and re-synced by the background job, which deletes the local copy after a successful upload.

## Layout
```
app/
  main.py config.py db.py models.py security.py serializers.py jobs.py cli.py
  api/        auth, me (+admin), groups (+periods), routing, expenses, settlements, attachments (+ocr), exports
  services/   splitting, balances, periods, resolve, people (claim/merge), seed, files, ocr, export, audit, access
  storage/    local, r2, service (R2 primary, local fallback)
migrations/   alembic
tests/
```
