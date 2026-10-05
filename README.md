# Mint AI demo boilerplate

Mint AI is a FastAPI backend and Telegram Mini App-style dashboard for a simulated server-investment platform. It includes provider-confirmed payment intents, an append-only ledger, Stars and normalized TON indexer webhook adapters, and an asynchronous withdrawal queue. Investment performance and dividends remain simulated. Do not use it to handle real funds until production-grade deposit and payout providers, monitoring, and a security review are in place.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
alembic upgrade head
uvicorn main:app --reload
```

The application and Alembic load values from `.env`. The default local database is `mintai.db`; migrations run before the API because automatic schema creation is disabled by default. Open <http://127.0.0.1:8000>. With demo authentication enabled, the default identity is user `10001`; use `?demo_user_id=10002` to switch demo users. For real Telegram Mini App authentication, configure `TELEGRAM_BOT_TOKEN` and set `ALLOW_DEMO_AUTH=false`.

To use PostgreSQL, set `DATABASE_URL` to an async SQLAlchemy URL, for example `postgresql+asyncpg://mintai:password@localhost:5432/mintai`, then run `alembic upgrade head`. Pool settings are configurable with `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `DB_POOL_TIMEOUT_SECONDS`, and `DB_POOL_RECYCLE_SECONDS`. SQLite enables foreign keys and a configurable busy timeout.

## Admin dashboard

The Streamlit admin dashboard is a separate app from the Telegram Mini App and runs independently while using the FastAPI service for all admin data and actions. Install the dependencies, set `ADMIN_TOKEN` in `.env`, and set `ADMIN_API_URL` to the API URL reachable by the Streamlit server (default `http://127.0.0.1:8000`). Start the API in one terminal and the dashboard in another:

```bash
# Terminal 1
uvicorn main:app --reload

# Terminal 2
streamlit run admin/app.py
```

Open the Streamlit URL shown in the terminal (usually <http://localhost:8501>) and connect with the admin token. The dashboard shows user and server-slot counts, financial summaries by currency, and manual-review withdrawals. It records Stars payout settlements and reconciles actual network fees for crypto withdrawals through the protected `/admin` API routes. Currency totals are not converted. Keep the dashboard private, use HTTPS and a strong admin token in production, and do not expose the token in client-side code.

## Payment flow

1. `POST /api/payment-intents` snapshots the server tier price and returns payment instructions. Pass an `Idempotency-Key` header. The server selects the server's fixed tier; clients cannot set the amount.
2. Stars intents use a Telegram invoice link and are confirmed only by the Telegram webhook. Set `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, and optionally `TELEGRAM_WEBHOOK_PATH`. Configure Telegram's webhook to that path and send the configured secret token header.
3. Crypto intents support TON and USDT on TON. Configure `CRYPTO_DEPOSIT_ADDRESS`, `TON_NETWORK`, `TON_INDEXER_WEBHOOK_SECRET`, and (for USDT) `USDT_JETTON_MASTER`. The indexer posts a normalized transfer event to `POST /webhooks/ton/indexer` with `X-Ton-Signature: <hex HMAC-SHA256 of the raw request body>`. The API verifies the signature, network, confirmations, destination, jetton master, exact amount, and memo before confirming. No chain-indexer client is included; an external indexer must produce this event shape:

```json
{
  "event_id": "provider-event-id",
  "intent_id": "payment-intent-uuid",
  "tx_hash": "transaction-hash",
  "tx_log_index": 0,
  "amount": "4.5",
  "currency": "USDT",
  "memo": "MINT-0123456789ABCDEF",
  "to_address": "configured-deposit-address",
  "token_master": "configured-jetton-master",
  "confirmations": 1,
  "network": "mainnet"
}
```

The `token_master` field is required for USDT and must be `null` for native TON. TON amounts are quoted from CoinGecko and stored on the intent; USDT uses the fixed USD tier price. Server principal stays in the locked wallet until the account reaches 50 qualified referrals. XTR purchases show a 21-day clearance timer; once it expires, the principal remains locked. USDT and TON principal remains locked without a timer. Withdrawals are paused while any XTR slot is within its clearance period.

`POST /api/investments` is confirm-only: it never accepts a client assertion as payment confirmation. Provider webhooks perform confirmation and create the slot, commission, telemetry, referral milestones, and ledger entries in one database transaction. A direct referrer receives a one-time 30% commission on the referred investor's first confirmed deposit only. A qualified referral is a referred account with a confirmed deposit.

The six server listings pay configured simulated daily rates from 20% through 45%. Before the first referral commission, ghost dividends are a non-withdrawable projection on the original net principal; they reset to principal every Monday at 00:00 UTC+3 and reset to the total same-currency principal when another server is purchased. After a first referral commission, ghost projections pause until a five-referral milestone unlocks true dividends. Each additional five qualified referrals grants the next non-overlapping five-day window; true dividends are credited once at each UTC midnight during a window. Ghost projections resume between windows and never enter the ledger. At 50 qualified referrals, each locked slot's net principal transfers once to the withdrawable wallet. Partial and full withdrawals both retain the existing 10% platform fee.

## Withdrawals and workers

`POST /api/withdrawal-quotes` creates a short-lived, one-use quote before money moves. The user sees the platform fee, estimated network fee, and expected payout, then confirms by submitting the quote ID to `POST /api/withdrawals` with an `Idempotency-Key`. Quotes expire after `WITHDRAWAL_QUOTE_TTL_SECONDS` (default 300 seconds); the API rechecks the Stars hold, wallet, and quote before atomically debiting the balance and crediting the 10% platform fee to the treasury.

XTR withdrawals are not sent through a fake crypto payout: they enter `manual_review` for an operator to redeem/convert via Telegram/Fragment and pay out in TON or USDT. Operators review them with `GET /admin/withdrawals` and settle them with `POST /admin/withdrawals/{id}/settle`, using `X-Admin-Token`; the settlement records the output currency/amount, conversion rate, actual TON gas fee, and transaction hash. Settlement is checked as converted XTR proceeds less actual gas; USDT settlements also require the TON/USD rate used for gas conversion. The quoted XTR payout is the post-platform-fee amount available for manual conversion; the actual exchange rate and gas deduction are determined when the operator processes it.

TON/USDT withdrawals reserve an estimated network fee in addition to the 10% platform fee. TON estimates are denominated in TON; USDT estimates convert native TON gas using the quote's TON/USD snapshot. The payout adapter must report both actual gas and the actual net amount sent; settlement validates that net payout equals the requested gross less the platform fee and actual gas charge. If the estimate differs from actual gas, the final payout amount changes accordingly. If a provider returns a fee or amount that cannot be reconciled, the transfer is held for administrator reconciliation at `POST /admin/withdrawals/{id}/reconcile-network-fee` rather than retried or refunded. Definitive payout failure reverses the platform fee and refunds the full requested balance. The demo adapter uses `DEMO_TON_GAS_FEE_ESTIMATE` and `DEMO_TON_USD_ESTIMATE` and deterministic simulated transfers. The non-demo default has no fee-quote or wallet-sending provider and rejects quote creation before debiting funds. A production adapter must quote fees before accepting the withdrawal, report actual network usage and net payout, and honor the payout idempotency key. `GET /api/withdrawals/{withdrawal_id}` returns the request and settlement state.

Background workers expire unpaid intents, process withdrawal jobs, and accrue simulated dividends at eligible UTC midnights. Set `ENABLE_BACKGROUND_WORKERS=false` to run API-only. Run one worker-enabled application instance unless withdrawal and dividend jobs are moved to a separately coordinated worker deployment.

## API and UI

- `GET /api/dashboard` — balances, server slots, referral link, and recent financial events.
- `GET /api/pricing` — six server listings with fixed tier assignments and current TON/USD quote.
- `/servers` and `/servers/{server-id}` — nested catalog and server detail routes; images and telemetry are illustrative.
- `POST /api/register` — initialize a Telegram/demo user and optionally attach an initial referral code.
- `PUT /api/profile` — set the payout wallet address.
- `POST /api/payment-intents` and `GET /api/payment-intents/{intent_id}` — create and inspect payment intents.
- `POST /api/investments` — confirm-only application endpoint for an already-confirmed intent.
- `POST /api/withdrawal-quotes`, `POST /api/withdrawals`, and `GET /api/withdrawals/{withdrawal_id}` — quote, request, and inspect withdrawals.
- `GET /admin/withdrawals`, `POST /admin/withdrawals/{withdrawal_id}/settle`, and `POST /admin/withdrawals/{withdrawal_id}/reconcile-network-fee` — review Stars withdrawals and reconcile actual crypto gas.
- `GET /admin/metrics` — aggregate profitability metrics, protected by `X-Admin-Token`.
- `WS /ws/live-wheel` — simulated commission ticker events.
- `POST /api/slots/{slot_id}/close` — archive a cleared slot; archived slots no longer accrue simulated dividends.

The six listings map in order to 300 Stars / $4.50, 1,000 / $15, 10,000 / $150, 100,000 / $1,500, 1,000,000 / $15,000, and 10,000,000 / $150,000. A deterministic 6-character referral code is derived from the Telegram user ID and `REFERRAL_SALT`; the finite code space can collide, in which case registration rejects the collision instead of changing the code.

## Operations and limitations

Set a long random `REFERRAL_SALT`, `ADMIN_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, and `TON_INDEXER_WEBHOOK_SECRET`; restrict `CORS_ORIGINS` to the hosted origin. Keep demo authentication and demo payments disabled in production. Currency totals in the admin endpoint are not converted. The TON adapter validates signed normalized events but is not an indexer integration. No live TON/USDT transfer implementation is included. Manual Stars/Fragment settlement is recorded by an authenticated administrator; this API does not redeem Stars or broadcast that payout. Migrations `0001`–`0003` include the financial baseline, withdrawal settlement, and referral-principal unlock tracking; back up the database before upgrading.

For repeatable synthetic accounts and local logic checks, see [STAGING.md](STAGING.md). The seeder requires a separate SQLite file with `staging` in its name and never deletes existing rows.
