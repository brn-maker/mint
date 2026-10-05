# Mint AI staging rehearsal

This is a local staging rehearsal for the demo backend. It uses a separate SQLite database and synthetic accounts. It does not connect a wallet, send funds, or contact Telegram's payment API. Keep it separate from any database or credentials that contain real user or payment data.

## 1. Create the staging accounts

From the project folder, run:

```bash
.venv/bin/python scripts/seed_staging_users.py \
  --database-url 'sqlite+aiosqlite:////tmp/mintai-staging.db'
```

The seeder refuses non-SQLite databases and SQLite filenames without `staging` in the name. It uses stable IDs, labels every account `stage_*`, never deletes rows, and is safe to rerun against the same staging file. The five demo IDs are printed at the end.

| Account | What to inspect |
| --- | --- |
| `dashboard` | Empty dashboard and server catalog |
| `withdrawals` | XTR, TON, and USDT balances with a sample payout address |
| `stars_hold` | A Stars slot still inside its 21-day clearance period |
| `five_referrals` | Five paid referrals; the dashboard refresh creates the first referral dividend window |
| `fifty_referrals` | Fifty paid referrals and a locked-principal slot; the dashboard refresh unlocks that principal once |

The referral fixtures are simulated confirmed intents, not actual payment events. Their purpose is to load repeatable account states into the UI.

## 2. Start the app against that database

Use a separate terminal in the project folder:

```bash
DATABASE_URL='sqlite+aiosqlite:////tmp/mintai-staging.db' \
ALLOW_DEMO_AUTH=true \
ALLOW_DEMO_PAYMENTS=true \
ENABLE_BACKGROUND_WORKERS=false \
.venv/bin/uvicorn main:app --reload
```

Open `http://127.0.0.1:8000/?demo_user_id=900000002` and replace the ID with one printed by the seeder to view another account. Worker processing is disabled here so test balances and queued payouts do not change in the background. The app still uses its configured demo payout adapter if a withdrawal is submitted; that adapter records simulated outcomes only.

## 3. Run the bug-hunt checks

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
```

The existing dividend tests and `test_staging_bug_hunt.py` use fresh in-memory SQLite databases. They check duplicate payment confirmation, one-time referral commissions, ledger idempotency and overdraft rejection, referral milestones and principal unlock, Stars clearance holds, single-use withdrawal quotes, simulated settlement, payout failure refunds, Telegram init-data signatures, and TON webhook signatures/network validation.

These checks validate the implemented demo rules. Passing them does not certify live-money readiness: the app still needs real provider integrations, deployment-specific secrets and webhook configuration, operational monitoring, and an independent security/accounting review before handling real funds.
