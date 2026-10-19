# Mint AI production setup

Mint AI currently prices deposits, credits, balances, and withdrawals in native TON only. Server prices are fixed TON amounts and do not use a live market conversion. Telegram is used for Mini App authentication; Telegram payment invoices are not part of the payment flow.

**Do not accept real funds.** TON deposits and the optional payout adapter are restricted to testnet. Do not configure a mainnet wallet or use this application to handle real payments.

## 1. Supabase PostgreSQL database

Use the Supabase PostgreSQL connection string in Render's `DATABASE_URL` setting. For a long-running Render API service, prefer Supabase's session pooler or direct connection with SSL as appropriate for your network setup; use the connection string format accepted by SQLAlchemy/asyncpg:

```text
postgresql+asyncpg://USER:PASSWORD@HOST:PORT/postgres?ssl=require
```

Keep credentials in Render's environment settings, not in the repository. The Render Blueprint in `render.yaml` requires `DATABASE_URL` to be supplied as a secret; it does not provision a separate Render database.

Run migrations as the Render build step (`alembic upgrade head`) or from a trusted deployment environment. Do not enable automatic schema creation in production.

## 2. Render API and Telegram Mini App

Deploy the repository using its `render.yaml` Blueprint or create a Python web service with:

- Build: `npm ci --no-audit --no-fund && pip install -r requirements.txt && alembic upgrade head`
- Start: `uvicorn main:app --host 0.0.0.0 --port $PORT`
- Health check: `/healthz`

Set the Mini App's HTTPS URL in BotFather. Configure `TELEGRAM_BOT_TOKEN` and `TELEGRAM_BOT_USERNAME` in Render; set `ALLOW_DEMO_AUTH=false`. This token validates Telegram Mini App init data only—no Telegram payment webhook is needed.

Set `TELEGRAM_MINI_APP_SHORT_NAME` to the short name assigned to the Mini App in BotFather. Referral links then use `https://t.me/<bot>/<short-name>?startapp=<code>` and Telegram supplies the code as `start_param`. Without this setting, the app emits a bot `?start=<code>` link, which only works if the bot's `/start` handler opens the Mini App and forwards the payload. Restrict `CORS_ORIGINS` to the hosted app origin (or leave it empty when the frontend and API are served from the same origin).

Run exactly one worker-enabled API instance unless the background workers are separated and coordinated. Multiple instances with embedded workers can duplicate scheduled work.

## 3. TON testnet payment configuration

Payment scanning is testnet-only. Configure the receiving Wallet V5R1 testnet address, testnet RPC endpoint and API key, and the wallet mnemonic in Render:

```env
TON_DEPOSIT_ADDRESS=<your receiving address>
TON_NETWORK=testnet
TON_API_ENDPOINT=https://testnet.toncenter.com/api/v2/jsonRPC
TON_API_KEY=<testnet RPC API key>
APP_WALLET_MNEMONIC=<wallet mnemonic words>
TON_PAYMENT_SCAN_INTERVAL_SECONDS=10
```

The Node verifier uses `@ton/ton` and `@ton/crypto` to connect to the TON RPC endpoint, derive the receiving testnet Wallet V5R1 address from the mnemonic, and parse incoming native TON transaction comments. It refuses non-testnet configuration and a configured deposit address that does not match the derived wallet. The Python worker polls transaction history and confirms an intent only for its unique transaction comment and exact quoted amount. Never commit or expose `APP_WALLET_MNEMONIC` or `TON_API_KEY`; do not use a mainnet endpoint or send real funds.

## 4. TON testnet payout adapter

An opt-in testnet `PayoutAdapter` is implemented in `payments/ton_payout.py`. It uses the configured Wallet V5R1 app wallet to:

- estimate native TON network fees before funds are debited;
- send the quoted net amount to the user's registered payout address with a unique withdrawal comment;
- look up that comment before retrying, to avoid duplicate sends after an uncertain RPC result; and
- return the source transaction hash, actual network fee, and amount sent for existing reconciliation.

Live sends are disabled by default. For an isolated testnet deployment only, set `TON_TESTNET_PAYOUTS_ENABLED=true`, `TON_NETWORK=testnet`, and `ALLOW_DEMO_PAYMENTS=false`; the helper also requires an HTTPS testnet RPC endpoint and verifies the configured address against the mnemonic-derived Wallet V5R1 address. Keep this flag false outside the dedicated testnet environment. Existing 10% platform-fee and actual network-cost reconciliation apply. This adapter is not suitable for mainnet or production use.

## 5. Environment variables

Set secrets in Render's environment settings. A production baseline:

```env
TELEGRAM_BOT_TOKEN=<bot-token>
TELEGRAM_BOT_USERNAME=<bot-username>
TELEGRAM_MINI_APP_SHORT_NAME=<botfather-mini-app-short-name>
ALLOW_DEMO_AUTH=false
ALLOW_DEMO_PAYMENTS=false
TON_TESTNET_PAYOUTS_ENABLED=false
REFERRAL_SALT=<long-random-secret>
ADMIN_TOKEN=<long-random-secret>
CORS_ORIGINS=https://<mini-app-host>

DATABASE_URL=<Supabase PostgreSQL async connection string>
AUTO_CREATE_SCHEMA=false
SQL_ECHO=false

TON_DEPOSIT_ADDRESS=<TON testnet receiving address>
TON_NETWORK=testnet
TON_API_ENDPOINT=https://testnet.toncenter.com/api/v2/jsonRPC
TON_API_KEY=<testnet RPC API key>
APP_WALLET_MNEMONIC=<wallet mnemonic words>
TON_PAYMENT_SCAN_INTERVAL_SECONDS=10
PAYMENT_INTENT_TTL_MINUTES=60
WITHDRAWAL_QUOTE_TTL_SECONDS=300

ENABLE_BACKGROUND_WORKERS=true
WITHDRAWAL_WORKER_INTERVAL_SECONDS=15
DIVIDEND_BATCH_INTERVAL_SECONDS=300
PAYMENT_EXPIRY_INTERVAL_SECONDS=60
WITHDRAWAL_PROCESSING_STALE_SECONDS=300
RATE_LIMIT_PER_MINUTE=30
```

`DEMO_TON_GAS_FEE_ESTIMATE` is only used by demo-mode simulated withdrawals. No Telegram payment webhook, USD price feed, or jetton master is used by the TON-only flow.

## 6. Test safely before launch

1. Deploy to a separate staging environment and separate database.
2. Keep demo auth and payments enabled only in that isolated environment.
3. Use a dedicated TON testnet wallet and RPC key; validate mnemonic/address matching, transaction comments, exact amounts, and duplicate transaction handling. Never use a mainnet wallet mnemonic in staging.
4. Test payouts only on TON testnet with a dedicated test wallet and explicitly enabled `TON_TESTNET_PAYOUTS_ENABLED`; test retries and reconciliation before any broader use. Never test payouts with production credentials or seed balances.
5. Verify the production build, migrations, backups, logs, alerting, and admin access controls.
6. Do not import or convert historical synthetic balances into TON. Legacy database fields are retained for compatibility; only newly confirmed native TON deposits should fund TON balances.

Use the isolated Supabase staging procedure in [STAGING.md](STAGING.md). Regression tests use disposable in-memory databases; the application itself requires PostgreSQL.
