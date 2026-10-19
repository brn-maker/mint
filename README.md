# Mint AI demo boilerplate

Mint AI is a FastAPI backend and Telegram Mini App-style dashboard for a simulated server-investment platform. It includes TON-only payment intents, a testnet transaction scanner, an append-only ledger, and an asynchronous withdrawal queue. Investment performance and dividends remain simulated. Do not use it to handle real funds; deposits are restricted to TON testnet and withdrawals remain simulated or unconfigured.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
npm ci --no-audit --no-fund
pip install -r requirements.txt
cp .env.example .env
alembic upgrade head
uvicorn main:app --reload
```

The application requires `DATABASE_URL` to point to PostgreSQL; configure the Supabase connection string in `.env`. It will not create or connect to a local SQLite database. Run migrations before starting the API because automatic schema creation is disabled by default. Open <http://127.0.0.1:8000>. With demo authentication enabled, the default identity is user `10001`; use `?demo_user_id=10002` to switch demo users. For real Telegram Mini App authentication, configure `TELEGRAM_BOT_TOKEN` and set `ALLOW_DEMO_AUTH=false`.

Use Supabase's PostgreSQL connection URL in async SQLAlchemy format (`postgresql+asyncpg://`) with SSL, then run `alembic upgrade head`. Pool settings are configurable with `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `DB_POOL_TIMEOUT_SECONDS`, and `DB_POOL_RECYCLE_SECONDS`.

## Deploy on Render

This repo includes `render.yaml` for a Render Blueprint. Configure its required `DATABASE_URL` secret with the Supabase PostgreSQL connection string. The service runs `alembic upgrade head` during the build and starts with:

```bash
uvicorn main:app --host 0.0.0.0 --port $PORT
```

If you configure the service manually instead of using the Blueprint, set these values in Render:

- Build command: `npm ci --no-audit --no-fund && pip install -r requirements.txt && alembic upgrade head`
- Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
- `DATABASE_URL`: Supabase PostgreSQL connection string, not the Supabase project API URL
- `AUTO_CREATE_SCHEMA=false`
- `ENABLE_BACKGROUND_WORKERS=true` for exactly one running instance, or `false` for API-only instances

Do not use `uvicorn main:app --reload` on Render. `--reload` is a local development option and can cause Render deploys to hang or time out.

## Admin dashboard

The Streamlit admin dashboard is a separate app from the Telegram Mini App and runs independently while using the FastAPI service for all admin data and actions. Install the dependencies, set `ADMIN_TOKEN` in `.env`, and set `ADMIN_API_URL` to the API URL reachable by the Streamlit server (default `http://127.0.0.1:8000`). Start the API in one terminal and the dashboard in another:

```bash
# Terminal 1
uvicorn main:app --reload

# Terminal 2
streamlit run admin/app.py
```

Open the Streamlit URL shown in the terminal (usually <http://localhost:8501>) and connect with the admin token. The dashboard shows user and server-slot counts, financial summaries, and TON withdrawals awaiting payout reconciliation. Keep the dashboard private, use HTTPS and a strong admin token in production, and do not expose the token in client-side code.

## Payment flow

1. `POST /api/payment-intents` snapshots the server tier price and returns payment instructions. Pass an `Idempotency-Key` header. The server selects the server's fixed tier; clients cannot set the amount.
2. Deposits use native TON testnet only. Configure `TON_DEPOSIT_ADDRESS`, `TON_API_ENDPOINT`, `TON_API_KEY`, and `APP_WALLET_MNEMONIC`; the address must be the testnet Wallet V5R1 address derived from the mnemonic. Use a Toncenter-compatible HTTPS testnet RPC endpoint. The background worker scans incoming wallet transactions, parses native TON comments, and confirms an intent only when its unique comment and exact amount match. Keep the API key and mnemonic server-side and never send real funds.

Server tier prices are fixed TON amounts: 4.50, 15, 150, 1,500, 15,000, and 150,000 TON. No external price feed is used. Server principal stays in the locked wallet until the account reaches 50 qualified referrals.

`POST /api/investments` is confirm-only: it never accepts a client assertion as payment confirmation. The testnet scanner confirms matching transfers and creates the slot, commission, telemetry, referral milestones, and ledger entries in one database transaction. A direct referrer receives a one-time 30% commission on the referred investor's first confirmed deposit only. A qualified referral is a referred account with a confirmed deposit.

Referral codes are stable six-character codes derived from the Telegram user ID and the secret `REFERRAL_SALT`; registration checks uniqueness and retries a different deterministic candidate on collision. `TELEGRAM_MINI_APP_SHORT_NAME` makes links open the Mini App directly using `startapp=CODE`. Without it, the link is a bot `start=CODE` deep link, and the bot must launch the Mini App while forwarding that parameter. A referral is attached only when the account is first created and cannot be reassigned later. Referred users qualify after their first confirmed TON deposit; groups of five qualified referrals grant five-day dividend windows, and 50 qualified referrals unlock server principal.

The six server listings pay configured simulated daily rates from 20% through 45%. Before the first referral commission, ghost dividends are a non-withdrawable projection on the original net principal; they reset to principal every Monday at 00:00 UTC+3 and reset to the total same-currency principal when another server is purchased. After a first referral commission, ghost projections pause until a five-referral milestone unlocks true dividends. Each additional five qualified referrals grants the next non-overlapping five-day window; true dividends are credited once at each UTC midnight during a window. Ghost projections resume between windows and never enter the ledger. At 50 qualified referrals, each locked slot's net principal transfers once to the withdrawable wallet. Partial and full withdrawals both retain the existing 10% platform fee.

## Withdrawals and workers

`POST /api/withdrawal-quotes` creates a short-lived, one-use TON quote before money moves. The user sees the 10% platform fee, estimated network fee, and expected payout, then confirms by submitting the quote ID to `POST /api/withdrawals` with an `Idempotency-Key`. Quotes expire after `WITHDRAWAL_QUOTE_TTL_SECONDS` (default 300 seconds). The payout adapter must report actual network usage and net payout; mismatched results require administrator reconciliation. Definitive payout failure refunds the user balance and reverses the platform fee. The demo adapter simulates transfers using `DEMO_TON_GAS_FEE_ESTIMATE`; the non-demo default has no live wallet provider and rejects quote creation. `GET /api/withdrawals/{withdrawal_id}` returns the request and settlement state.

Background workers scan TON testnet payments, expire unpaid intents, process withdrawal jobs, and accrue simulated dividends at eligible UTC midnights. Set `ENABLE_BACKGROUND_WORKERS=false` to run API-only. Run one worker-enabled application instance unless withdrawal and dividend jobs are moved to a separately coordinated worker deployment.

## API and UI

- `GET /api/dashboard` — balances, server slots, referral link, and recent financial events.
- `GET /api/pricing` — six server listings with fixed TON prices.
- `/servers` and `/servers/{server-id}` — nested catalog and server detail routes; images and telemetry are illustrative.
- `POST /api/register` — initialize a Telegram/demo user and optionally attach an initial referral code.
- `PUT /api/profile` — set the payout wallet address.
- `POST /api/payment-intents` and `GET /api/payment-intents/{intent_id}` — create and inspect payment intents.
- `POST /api/investments` — confirm-only application endpoint for an already-confirmed intent.
- `POST /api/withdrawal-quotes`, `POST /api/withdrawals`, and `GET /api/withdrawals/{withdrawal_id}` — quote, request, and inspect withdrawals.
- `GET /admin/withdrawals` and `POST /admin/withdrawals/{withdrawal_id}/reconcile-network-fee` — review TON payouts and reconcile actual network fees.
- `GET /admin/metrics` — aggregate profitability metrics, protected by `X-Admin-Token`.
- `WS /ws/live-wheel` — simulated commission ticker events.
- `POST /api/slots/{slot_id}/close` — archive a cleared slot; archived slots no longer accrue simulated dividends.

The six listings map to 4.50, 15, 150, 1,500, 15,000, and 150,000 TON.

## Operations and limitations

Keep `TON_NETWORK=testnet`; the scanner rejects other networks and rejects RPC endpoints that are not HTTPS testnet endpoints. Store `TON_API_KEY` and `APP_WALLET_MNEMONIC` only in server-side environment settings. The mnemonic-derived Wallet V5R1 address must match `TON_DEPOSIT_ADDRESS`. The scanner is not a production deposit integration, withdrawals remain simulated or unconfigured, and investment performance is illustrative. Legacy database columns are retained for compatibility; do not convert historical non-TON balances. Migrations `0001`–`0003` include the financial baseline, withdrawal settlement, and referral-principal unlock tracking; back up the database before upgrading.

For a separate Supabase staging environment and local logic checks, see [STAGING.md](STAGING.md). Tests use disposable in-memory databases; the app itself only connects to PostgreSQL.
