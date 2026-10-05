# Mint AI Production Setup Guide

This guide walks you through configuring real APIs and connections for production deployment.

---

## 1. Database: PostgreSQL

### Create Database & User
```bash
sudo -u postgres psql <<'SQL'
CREATE DATABASE mintai;
CREATE USER mintai WITH ENCRYPTED PASSWORD 'strong-random-password';
GRANT ALL PRIVILEGES ON DATABASE mintai TO mintai;
\c mintai
GRANT ALL ON SCHEMA public TO mintai;
SQL
```

### Update `.env`
```env
DATABASE_URL=postgresql+asyncpg://mintai:strong-random-password@localhost:5432/mintai
AUTO_CREATE_SCHEMA=false
DB_POOL_SIZE=10
DB_MAX_OVERFLOW=20
DB_POOL_TIMEOUT_SECONDS=30
DB_POOL_RECYCLE_SECONDS=1800
SQL_ECHO=false
```

### Run Migrations
```bash
alembic upgrade head
```

---

## 2. Telegram Bot Setup

### Create Bot
1. Message `@BotFather` on Telegram
2. `/newbot` → follow prompts
3. Save the **Bot Token** (format: `123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ`)

### Set Webhook
```bash
# Replace with your production domain
WEBHOOK_URL="https://api.yourdomain.com/webhooks/telegram"
BOT_TOKEN="your-bot-token-here"

curl -X POST "https://api.telegram.org/bot${BOT_TOKEN}/setWebhook" \
  -H "Content-Type: application/json" \
  -d "{\"url\": \"${WEBHOOK_URL}\", \"secret_token\": \"your-webhook-secret-here\", \"allowed_updates\": [\"message\", \"pre_checkout_query\"]}"
```

### Configure Bot for Stars Payments
```bash
# Enable Stars payments
curl -X POST "https://api.telegram.org/bot${BOT_TOKEN}/setMyCommands" \
  -H "Content-Type: application/json" \
  -d '{"commands": [{"command": "start", "description": "Open Mint AI"}]}'

# Verify webhook
curl "https://api.telegram.org/bot${BOT_TOKEN}/getWebhookInfo"
```

### Update `.env`
```env
TELEGRAM_BOT_TOKEN=123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ
TELEGRAM_BOT_USERNAME=mintai_bot
TELEGRAM_WEBHOOK_PATH=/webhooks/telegram
TELEGRAM_WEBHOOK_SECRET=your-32-char-random-webhook-secret
TELEGRAM_API_BASE=https://api.telegram.org
ALLOW_DEMO_AUTH=false
TELEGRAM_INIT_DATA_MAX_AGE_SECONDS=86400
```

---

## 3. TON Blockchain Configuration

### Get TON Deposit Address
- Use a custodial wallet (Tonkeeper, Tonhub) or run your own wallet
- **Must be a single address** that receives both TON and USDT deposits
- Format: `EQ...` or `UQ...` (user-friendly) or `0:...` (raw)

### Configure TON Indexer Webhook
You need an external indexer (e.g., **Toncenter**, **TON API**, **Dedust**, or self-hosted) that:
1. Monitors your deposit address
2. Posts normalized events to `POST /webhooks/ton/indexer`
3. Signs payloads with HMAC-SHA256

**Required Event Schema:**
```json
{
  "event_id": "provider-unique-id",
  "intent_id": "payment-intent-uuid-from-your-api",
  "tx_hash": "transaction-hash-hex",
  "tx_log_index": 0,
  "amount": "4.5",
  "currency": "TON",
  "memo": "MINT-ABCDEF1234567890",
  "to_address": "your-deposit-address",
  "token_master": null,
  "confirmations": 1,
  "network": "mainnet"
}
```

**For USDT, `token_master` must equal your USDT jetton master address.**

### Update `.env`
```env
CRYPTO_DEPOSIT_ADDRESS=EQYourDepositAddressHere
TON_NETWORK=mainnet
TON_MIN_CONFIRMATIONS=1
TON_INDEXER_WEBHOOK_SECRET=your-32-char-random-indexer-secret
USDT_JETTON_MASTER=EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs
```

---

## 4. USDT Jetton Master (TON Mainnet)

The USDT jetton master on TON mainnet is:
```
EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs
```

Verify at: https://tonscan.org/jetton/EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs

---

## 5. Pricing: CoinGecko API

The app uses CoinGecko's free tier for TON/USD pricing:
- Endpoint: `https://api.coingecko.com/api/v3/simple/price?ids=the-open-network&vs_currencies=usd`
- Rate limit: 10-50 calls/minute (free tier)
- Cached for 60 seconds in code

**Optional: Upgrade to Pro** for higher limits and SLA.

---

## 6. Environment Configuration

### Complete Production `.env`
```env
# ===========================================
# TELEGRAM
# ===========================================
TELEGRAM_BOT_TOKEN=123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ
TELEGRAM_BOT_USERNAME=mintai_bot
TELEGRAM_WEBHOOK_PATH=/webhooks/telegram
TELEGRAM_WEBHOOK_SECRET=webhook-secret-32-chars-min
TELEGRAM_API_BASE=https://api.telegram.org

# ===========================================
# AUTH & SECURITY
# ===========================================
REFERRAL_SALT=super-long-random-referral-salt-64-chars-minimum
ADMIN_TOKEN=super-long-random-admin-token-64-chars-minimum
ADMIN_API_URL=https://api.yourdomain.com
ALLOW_DEMO_AUTH=false
ALLOW_DEMO_PAYMENTS=false
CORS_ORIGINS=https://app.yourdomain.com,https://admin.yourdomain.com

# ===========================================
# DATABASE
# ===========================================
DATABASE_URL=postgresql+asyncpg://mintai:password@localhost:5432/mintai
AUTO_CREATE_SCHEMA=false
DB_POOL_SIZE=10
DB_MAX_OVERFLOW=20
DB_POOL_TIMEOUT_SECONDS=30
DB_POOL_RECYCLE_SECONDS=1800
SQL_ECHO=false
SQLITE_BUSY_TIMEOUT_SECONDS=30

# ===========================================
# TON / CRYPTO
# ===========================================
CRYPTO_DEPOSIT_ADDRESS=EQYourDepositAddressHere
TON_NETWORK=mainnet
TON_MIN_CONFIRMATIONS=1
TON_INDEXER_WEBHOOK_SECRET=indexer-secret-32-chars-min
USDT_JETTON_MASTER=EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs

# ===========================================
# PAYMENT TIMING
# ===========================================
PAYMENT_QUOTE_MINUTES=30
PAYMENT_INTENT_TTL_MINUTES=60
WITHDRAWAL_QUOTE_TTL_SECONDS=300

# ===========================================
# BACKGROUND WORKERS
# ===========================================
ENABLE_BACKGROUND_WORKERS=true
WITHDRAWAL_WORKER_INTERVAL_SECONDS=15
DIVIDEND_BATCH_INTERVAL_SECONDS=300
PAYMENT_EXPIRY_INTERVAL_SECONDS=60
WITHDRAWAL_PROCESSING_STALE_SECONDS=300
RATE_LIMIT_PER_MINUTE=30

# ===========================================
# DEMO VALUES (REPLACE WITH REAL PROVIDERS)
# ===========================================
DEMO_TON_GAS_FEE_ESTIMATE=0.005
DEMO_TON_USD_ESTIMATE=5.0
```

---

## 7. Payout Provider Implementation

**Critical:** The current `SimulatedPayoutAdapter` only simulates transfers. You **must** implement a real `PayoutAdapter` for production.

### Interface to Implement (`payments/payouts.py`):
```python
class PayoutAdapter(Protocol):
    async def quote_network_fee(self, currency: str, payout_amount: Decimal) -> NetworkFeeQuote: ...
    async def send(self, withdrawal: WithdrawalRequest, *, idempotency_key: str) -> PayoutResult: ...
```

### Requirements:
1. **TON Payouts**: Send native TON from your treasury wallet
2. **USDT Payouts**: Send USDT jetton from your treasury wallet
3. **Report actual gas used** and **net amount sent**
4. **Honor idempotency key** for retries
5. **Return tx_hash** for on-chain verification

### Integration Points:
- Replace `get_payout_adapter()` in `payments/payouts.py`
- Treasury wallet needs TON + USDT balance
- Monitor for failed payouts (auto-refund logic exists)

---

## 8. Webhook Endpoints Configuration

### Telegram Webhook
```
POST https://api.yourdomain.com/webhooks/telegram
Headers: X-Telegram-Bot-Api-Secret-Token: your-webhook-secret
```

### TON Indexer Webhook
```
POST https://api.yourdomain.com/webhooks/ton/indexer
Headers: X-Ton-Signature: sha256=hmac-sha256-of-body
```

### Test Webhooks Locally (ngrok)
```bash
ngrok http 8000
# Use ngrok URL for webhook testing
```

---

## 9. Background Workers

### Run Workers Separately (Recommended)
```bash
# Terminal 1: API server
ENABLE_BACKGROUND_WORKERS=false uvicorn main:app --host 0.0.0.0 --port 8000

# Terminal 2: Workers
ENABLE_BACKGROUND_WORKERS=true python -m workers.runner
```

### Or Single Process (Development Only)
```bash
ENABLE_BACKGROUND_WORKERS=true uvicorn main:app --host 0.0.0.0 --port 8000
```

---

## 10. Admin Dashboard

### Configure
```env
ADMIN_TOKEN=your-admin-token
ADMIN_API_URL=https://api.yourdomain.com
```

### Run
```bash
cd admin
streamlit run app.py --server.port 8501 --server.address 0.0.0.0
```

### Secure
- Put behind VPN or IP allowlist
- Use HTTPS with valid certs
- Never expose admin token in client code

---

## 11. SSL / Reverse Proxy (Nginx Example)

```nginx
server {
    listen 443 ssl http2;
    server_name api.yourdomain.com;

    ssl_certificate /etc/letsencrypt/live/yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/yourdomain.com/privkey.pem;

    # API
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # Admin (restrict access)
    location /admin/ {
        allow 1.2.3.4;  # Your office IP
        deny all;
        proxy_pass http://127.0.0.1:8501;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

---

## 12. Testing with Seed Users

### After Full Configuration:
```bash
# 1. Start API with workers
ENABLE_BACKGROUND_WORKERS=true uvicorn main:app --host 0.0.0.0 --port 8000

# 2. Seed staging users (uses your real DB)
.venv/bin/python scripts/seed_staging_users.py \
  --database-url 'postgresql+asyncpg://mintai:password@localhost:5432/mintai'

# 3. Test Telegram auth
# Open: https://app.yourdomain.com/?tgWebAppData=...
# Or use Telegram Mini App directly

# 4. Test payments
# - Create payment intent via API
# - Pay with Stars (Telegram) or TON/USDT (wallet)
# - Verify webhook confirmation
# - Check slot created in dashboard

# 5. Test withdrawals
# - Request quote
# - Confirm withdrawal
# - Verify payout (requires real payout adapter)
```

---

## 13. Monitoring & Alerts

### Key Metrics to Watch:
- Payment intent success/failure rates
- Webhook delivery success (Telegram + TON)
- Withdrawal queue depth
- Database connection pool usage
- TON/USD price feed availability
- Admin withdrawal queue (manual review)

### Health Checks:
```bash
# API health
curl https://api.yourdomain.com/api/pricing

# Database
psql -h localhost -U mintai -d mintai -c "SELECT 1"

# Workers
ps aux | grep workers.runner
```

---

## 14. Security Checklist

- [ ] `ALLOW_DEMO_AUTH=false`
- [ ] `ALLOW_DEMO_PAYMENTS=false`
- [ ] Strong `REFERRAL_SALT` (64+ chars)
- [ ] Strong `ADMIN_TOKEN` (64+ chars)
- [ ] Strong webhook secrets (32+ chars)
- [ ] `CORS_ORIGINS` restricted to your domains
- [ ] HTTPS everywhere (valid certs)
- [ ] Admin dashboard behind VPN/IP allowlist
- [ ] Database credentials rotated
- [ ] Telegram webhook secret verified
- [ ] TON indexer webhook secret verified
- [ ] Treasury wallet secured (multisig recommended)
- [ ] Rate limiting configured
- [ ] Log aggregation enabled
- [ ] Backup strategy for PostgreSQL

---

## 15. Rollout Plan

1. **Staging**: Deploy to staging env, test all flows with testnet
2. **Canary**: Enable for 1% of users, monitor closely
3. **Full**: Gradually increase traffic
4. **Rollback Plan**: Keep previous version deployable

---

## Files That Need Production Implementation

| File | What to Implement |
|------|-------------------|
| `payments/payouts.py` | Real `PayoutAdapter` for TON/USDT transfers |
| `payments/ton.py` | Optional: Custom indexer client if needed |
| `pricing.py` | Optional: Backup price feeds (Binance, CoinMarketCap) |
| `workers/runner.py` | Optional: Separate worker deployment config |

---

## Support Commands

```bash
# View logs
journalctl -u mintai-api -f

# Check migrations
alembic current
alembic history

# Manual webhook test
curl -X POST https://api.yourdomain.com/webhooks/telegram \
  -H "X-Telegram-Bot-Api-Secret-Token: your-secret" \
  -H "Content-Type: application/json" \
  -d '{"update_id":123,"pre_checkout_query":{"id":"test","from":{"id":10001},"invoice_payload":"intent-uuid","currency":"XTR","total_amount":300}}'

# Admin API test
curl -H "X-Admin-Token: your-admin-token" https://api.yourdomain.com/admin/metrics
```