import { Address, SendMode, internal, toNano } from "@ton/core";
import { mnemonicToWalletKey, mnemonicValidate } from "@ton/crypto";
import { TonClient, WalletContractV5R1 } from "@ton/ton";
import { stdin } from "node:process";

const NANO = 1_000_000_000n;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function fail(message, category = "rejected") {
  process.stdout.write(JSON.stringify({ error: message, category }));
  process.exitCode = 1;
}

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

function resolveV3Endpoint(endpoint) {
  const override = process.env.TON_API_V3_ENDPOINT?.trim();
  if (override) {
    return new URL(override);
  }
  const base = new URL(endpoint);
  return new URL(`${base.protocol}//${base.host}/api/v3`);
}

function validateEndpoint(endpoint, network) {
  let parsed;
  try {
    parsed = new URL(endpoint);
  } catch {
    throw new Error("TON_API_ENDPOINT must be a valid HTTPS RPC URL");
  }
  if (parsed.protocol !== "https:") {
    throw new Error("TON_API_ENDPOINT must be an HTTPS RPC endpoint");
  }
  const isTestnetHost = parsed.hostname
    .toLowerCase()
    .split(".")
    .some((label) => label === "testnet" || label.startsWith("testnet-"));
  if (network === "testnet" && !isTestnetHost) {
    throw new Error("TON_NETWORK=testnet requires a testnet RPC endpoint");
  }
  if (network === "mainnet" && isTestnetHost) {
    throw new Error("TON_NETWORK=mainnet requires a mainnet RPC endpoint");
  }
}

async function readInput() {
  let input = "";
  for await (const chunk of stdin) input += chunk;
  return JSON.parse(input);
}

function amountString(amount) {
  const whole = amount / NANO;
  const fraction = (amount % NANO).toString().padStart(9, "0").slice(0, 8);
  return `${whole}.${fraction}`;
}

async function setup() {
  const network = process.env.TON_NETWORK;
  const isTestnet = network === "testnet";
  const isMainnet = network === "mainnet";
  if (!isTestnet && !isMainnet) {
    throw new Error("TON_NETWORK must be 'testnet' or 'mainnet'");
  }
  const endpoint = process.env.TON_API_ENDPOINT;
  const apiKey = process.env.TON_API_KEY;
  const mnemonic = process.env.APP_WALLET_MNEMONIC?.trim().split(/\s+/);
  if (!endpoint || !apiKey || !mnemonic?.length) {
    throw new Error("TON RPC credentials and app wallet mnemonic are required");
  }
  validateEndpoint(endpoint, network);
  if (!(await mnemonicValidate(mnemonic))) {
    throw new Error("APP_WALLET_MNEMONIC is invalid");
  }

  const keyPair = await mnemonicToWalletKey(mnemonic);
  const wallet = WalletContractV5R1.create({
    publicKey: keyPair.publicKey,
    walletId: {
      networkGlobalId: isTestnet ? -3 : -239,
      context: { walletVersion: "v5r1", workchain: 0, subwalletNumber: 0 },
    },
  });
  let configuredAddress;
  try {
    configuredAddress = Address.parse(process.env.TON_DEPOSIT_ADDRESS);
  } catch {
    throw new Error("TON_DEPOSIT_ADDRESS is invalid");
  }
  if (!configuredAddress.equals(wallet.address)) {
    throw new Error("TON_DEPOSIT_ADDRESS does not match the derived Wallet V5R1");
  }
  const v3Endpoint = resolveV3Endpoint(endpoint);
  const client = new TonClient({ endpoint, apiKey, timeout: 15000 });
  return { client, wallet, keyPair, v3Endpoint, apiKey };
}

function feeAmount(fees) {
  const source = fees.source_fees;
  return BigInt(source.storage_fee)
    + BigInt(source.gas_fee)
    + BigInt(source.fwd_fee);
}

async function estimateFee(client, wallet, keyPair, destination, amount, comment) {
  const seqno = await client.open(wallet).getSeqno();
  const transfer = await wallet.createTransfer({
    seqno,
    secretKey: keyPair.secretKey,
    sendMode: SendMode.PAY_GAS_SEPARATELY,
    messages: [
      internal({
        to: destination,
        value: toNano(amount),
        bounce: false,
        body: comment,
      }),
    ],
  });
  const fees = await client.estimateExternalMessageFee(wallet.address, {
    body: transfer,
    initCode: null,
    initData: null,
    ignoreSignature: false,
  });
  return feeAmount(fees);
}

async function findExistingPayout(v3Endpoint, apiKey, wallet, destination, marker) {
  const account = wallet.address.toRawString().toLowerCase();
  const destinationRaw = destination.toRawString().toLowerCase();
  const params = new URLSearchParams({
    account,
    limit: "50",
    sort: "desc",
  });
  const url = `${v3Endpoint.toString().replace(/\/$/, "")}/transactions?${params.toString()}`;
  const data = await fetchJson(url, apiKey);
  const transactions = Array.isArray(data?.transactions) ? data.transactions : [];
  for (const transaction of transactions) {
    if (!Array.isArray(transaction.out_msgs)) continue;
    for (const outMsg of transaction.out_msgs) {
      const dest = String(outMsg.destination || "").toLowerCase();
      const comment = outMsg.message_content?.decoded?.comment;
      if (dest !== destinationRaw || comment !== marker) continue;
      if (transaction.description?.aborted === true) {
        throw new Error("Previously submitted payout was aborted");
      }
      return {
        tx_hash: Buffer.from(String(transaction.hash), "base64").toString("hex"),
        payout_amount: amountString(BigInt(String(outMsg.value ?? "0"))),
        payout_currency: "TON",
        network_fee_amount: amountString(BigInt(String(transaction.total_fees ?? "0"))),
        network_fee_currency: "TON",
      };
    }
  }
  return null;
}

async function main() {
  const input = await readInput();
  if (!input || !["quote", "send"].includes(input.operation)) {
    throw new Error("Unsupported payout operation");
  }
  if (input.currency && input.currency !== "TON") {
    throw new Error("TON payouts support TON only");
  }
  const { client, wallet, keyPair, v3Endpoint, apiKey } = await setup();
  const amount = Number(input.amount);
  if (!Number.isFinite(amount) || amount <= 0) {
    throw new Error("Payout amount must be finite and positive");
  }

  if (input.operation === "quote") {
    const destination = Address.parse(input.destination);
    if (destination.equals(wallet.address)) {
      throw new Error("Payout destination must not be the app wallet");
    }
    const fee = await estimateFee(
      client,
      wallet,
      keyPair,
      destination,
      input.amount,
      "Mint withdrawal 00000000-0000-0000-0000-000000000000",
    );
    const quote = amountString(fee);
    process.stdout.write(JSON.stringify({
      native_fee_amount: quote,
      withdrawal_fee_amount: quote,
    }));
    return;
  }

  if (
    typeof input.withdrawal_id !== "string"
    || !/^[0-9a-f-]{36}$/i.test(input.withdrawal_id)
    || input.idempotency_key !== `withdrawal:${input.withdrawal_id}`
  ) {
    throw new Error("Withdrawal idempotency data is invalid");
  }
  const destination = Address.parse(input.destination);
  if (destination.equals(wallet.address)) {
    throw new Error("Payout destination must not be the app wallet");
  }
  const marker = `Mint withdrawal ${input.withdrawal_id}`;
  let existing;
  try {
    existing = await findExistingPayout(v3Endpoint, apiKey, wallet, destination, marker);
  } catch {
    fail("Could not verify prior payout history", "pending");
    return;
  }
  if (existing) {
    process.stdout.write(JSON.stringify(existing));
    return;
  }

  const seqno = await client.open(wallet).getSeqno();
  const transfer = await wallet.createTransfer({
    seqno,
    secretKey: keyPair.secretKey,
    sendMode: SendMode.PAY_GAS_SEPARATELY,
    messages: [
      internal({
        to: destination,
        value: toNano(input.amount),
        bounce: false,
        body: marker,
      }),
    ],
  });
  try {
    await client.open(wallet).sendTransfer({
      seqno,
      secretKey: keyPair.secretKey,
      sendMode: SendMode.PAY_GAS_SEPARATELY,
      messages: [
        internal({
          to: destination,
          value: toNano(input.amount),
          bounce: false,
          body: marker,
        }),
      ],
    });
  } catch {
    fail("Payout submission outcome is unknown", "pending");
    return;
  }

  const deadline = Date.now() + 60_000;
  while (Date.now() < deadline) {
    try {
      const submitted = await findExistingPayout(v3Endpoint, apiKey, wallet, destination, marker);
      if (submitted) {
        process.stdout.write(JSON.stringify(submitted));
        return;
      }
    } catch {
      // A failed RPC lookup cannot prove whether the submitted message was accepted.
    }
    await sleep(2_000);
  }
  fail("Payout submitted but not yet visible; retry will check its unique on-chain comment", "pending");
}

main().catch((error) => fail(`TON payout helper failed: ${error?.message || error}`));
