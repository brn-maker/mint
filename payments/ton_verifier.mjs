import { Address } from "@ton/core";
import { mnemonicToWalletKey, mnemonicValidate } from "@ton/crypto";
import { WalletContractV5R1 } from "@ton/ton";

const fail = (message) => {
  process.stderr.write(`${message}\n`);
  process.exit(1);
};

async function fetchJson(url, apiKey) {
  const headers = { accept: "application/json" };
  if (apiKey) headers["X-API-Key"] = apiKey;
  const maxAttempts = 5;
  let lastError;
  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    try {
      const response = await fetch(url, { headers });
      if (response.status === 429 || response.status >= 500) {
        lastError = new Error(`HTTP ${response.status}`);
        const backoff = Math.min(5000, 750 * 2 ** attempt);
        await new Promise((resolve) => setTimeout(resolve, backoff));
        continue;
      }
      if (!response.ok) {
        let detail = "";
        try {
          detail = (await response.text()).slice(0, 300);
        } catch {}
        throw new Error(`HTTP ${response.status}${detail ? `: ${detail}` : ""}`);
      }
      return await response.json();
    } catch (error) {
      lastError = error;
      const backoff = Math.min(5000, 750 * 2 ** attempt);
      await new Promise((resolve) => setTimeout(resolve, backoff));
    }
  }
  throw lastError;
}

async function main() {
  const endpoint = process.env.TON_API_ENDPOINT;
  const apiKey = process.env.TON_API_KEY;
  const mnemonic = process.env.APP_WALLET_MNEMONIC?.trim().split(/\s+/);
  const configuredAddress = process.env.TON_DEPOSIT_ADDRESS;
  const network = process.env.TON_NETWORK;
  const since = Number(process.argv[2]);

  if (!endpoint || !apiKey || !mnemonic?.length || !configuredAddress) {
    fail("TON_API_ENDPOINT, TON_API_KEY, APP_WALLET_MNEMONIC, and TON_DEPOSIT_ADDRESS are required");
  }
  const isTestnet = network === "testnet";
  const isMainnet = network === "mainnet";
  if (!isTestnet && !isMainnet) fail("TON_NETWORK must be 'testnet' or 'mainnet'");

  // Resolve the v3 REST endpoint. Prefer an explicit override; otherwise derive it
  // from the v2 endpoint (https://<host>/api/v2/jsonRPC -> https://<host>/api/v3).
  const v3Endpoint = process.env.TON_API_V3_ENDPOINT?.trim();
  let endpointUrl;
  if (v3Endpoint) {
    try {
      endpointUrl = new URL(v3Endpoint);
    } catch {
      fail("TON_API_V3_ENDPOINT must be a valid URL");
    }
  } else {
    try {
      const base = new URL(endpoint);
      endpointUrl = new URL(`${base.protocol}//${base.host}/api/v3`);
    } catch {
      fail("TON_API_ENDPOINT must be a valid URL");
    }
  }
  const host = endpointUrl.hostname.toLowerCase();
  const isTestnetHost = host
    .split(".")
    .some((label) => label === "testnet" || label.startsWith("testnet-"));
  if (endpointUrl.protocol !== "https:") {
    fail("TON API endpoint must be HTTPS");
  }
  if (isTestnet && !isTestnetHost) {
    fail("TON_NETWORK=testnet requires a testnet RPC endpoint");
  }
  if (isMainnet && isTestnetHost) {
    fail("TON_NETWORK=mainnet requires a mainnet RPC endpoint");
  }

  if (!Number.isSafeInteger(since) || since < 0) fail("Transaction scan start time is invalid");
  if (!(await mnemonicValidate(mnemonic))) fail("APP_WALLET_MNEMONIC is invalid");

  const keyPair = await mnemonicToWalletKey(mnemonic);
  const wallet = WalletContractV5R1.create({
    workchain: 0,
    publicKey: keyPair.publicKey,
    walletId: {
      networkGlobalId: isTestnet ? -3 : -239,
      context: { walletVersion: "v5r1", workchain: 0, subwalletNumber: 0 },
    },
  });
  let expectedAddress;
  try {
    expectedAddress = Address.parse(configuredAddress);
  } catch {
    fail("TON_DEPOSIT_ADDRESS is invalid");
  }
  if (!expectedAddress.equals(wallet.address)) {
    fail("TON_DEPOSIT_ADDRESS does not match the Wallet V5R1 derived from APP_WALLET_MNEMONIC");
  }

  const account = wallet.address.toRawString();
  const accountLower = account.toLowerCase();
  // Match the prior v2 scan window: allow a small buffer before the intent's creation time.
  const startUtime = Math.max(0, since - 60);
  const pageSize = 100;
  const maxPages = 100;

  const transfers = [];
  for (let page = 0; page < maxPages; page += 1) {
    const params = new URLSearchParams({
      account,
      limit: String(pageSize),
      sort: "desc",
      start_utime: String(startUtime),
      offset: String(page * pageSize),
    });
    const url = `${endpointUrl.toString().replace(/\/$/, "")}/transactions?${params.toString()}`;
    const data = await fetchJson(url, apiKey);
    const transactions = Array.isArray(data?.transactions) ? data.transactions : [];
    if (!transactions.length) break;

    for (const transaction of transactions) {
      const message = transaction.in_msg;
      if (!message) continue;
      if (!message.destination || String(message.destination).toLowerCase() !== accountLower) continue;
      if (message.bounced) continue;

      const decoded = message.message_content?.decoded;
      const comment = typeof decoded?.comment === "string" ? decoded.comment : "";
      const bounceOutput = Array.isArray(transaction.out_msgs)
        && transaction.out_msgs.some((output) => output.bounced === true);

      transfers.push({
        tx_hash: Buffer.from(String(transaction.hash), "base64").toString("hex"),
        amount_nano: String(message.value ?? "0"),
        comment,
        incoming_bounced: message.bounced === true,
        bounce_output: bounceOutput === true,
      });
    }

    if (transactions.length < pageSize) break;
  }

  process.stdout.write(JSON.stringify(transfers));
}

main().catch((error) => fail(`TON transaction scan failed: ${error?.message || error}`));
