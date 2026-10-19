# Supabase staging

Use a separate Supabase project (or an isolated branch/database) for staging. Never point a staging deployment or seed data at the production project.

1. Copy the staging project's PostgreSQL connection string into the staging service's `DATABASE_URL` environment setting, formatted for async SQLAlchemy/asyncpg.
2. Set `AUTO_CREATE_SCHEMA=false`; deploy the API so its build step runs `alembic upgrade head`.
3. Use a separate Telegram bot and Mini App, with staging values for `TELEGRAM_BOT_TOKEN`, `TELEGRAM_BOT_USERNAME`, `TELEGRAM_MINI_APP_SHORT_NAME`, and `REFERRAL_SALT`.
4. Keep demo auth/payments isolated to staging. Do not seed balances intended to look like real funds.
5. Set `TON_NETWORK=testnet` and configure the dedicated testnet wallet's `TON_DEPOSIT_ADDRESS`, `TON_API_ENDPOINT`, `TON_API_KEY`, and `APP_WALLET_MNEMONIC`. Test a confirmed testnet deposit with the exact comment shown for an intent, then verify referral rewards. Never use a mainnet mnemonic or send mainnet funds.

Regression tests use fresh in-memory SQLite databases only as test fixtures; the application itself requires PostgreSQL and does not connect to a local SQLite database.

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
```
