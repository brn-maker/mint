import { Address } from "@ton/core";
import { mnemonicToWalletKey, mnemonicValidate } from "@ton/crypto";
import { TonClient, WalletContractV5R1 } from "@ton/ton";

const fail = (message) => {
  process.stderr.write(`${message}\n`);
  process.exit(1);
};

function readComment(body) {
  try {
    const slice = body.beginParse();
    if (slice.remainingBits < 32 || slice.loadUint(32) !== 0) return "";
    return slice.loadStringTail();
  } catch {
    return "";
  }
}

async function getTransactionsWithRetry(client, address, opts) {
  const maxAttempts = 8;
  let lastError;
  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    try {
      return await client.getTransactions(address, opts);
    } catch (error) {
      lastError = error;
      // Toncenter testnet lite servers can transiently fail with
      // "lt not in db" replication lag; back off and retry before giving up.
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
  if (network !== "testnet") fail("TON transaction scanning is restricted to TON_NETWORK=testnet");
  let endpointUrl;
  try {
    endpointUrl = new URL(endpoint);
  } catch {
    fail("TON_API_ENDPOINT must be a valid URL");
  }
  const testnetHost = endpointUrl.hostname
    .toLowerCase()
    .split(".")
    .some((label) => label === "testnet" || label.startsWith("testnet-"));
  if (endpointUrl.protocol !== "https:" || !testnetHost) {
    fail("TON_API_ENDPOINT must be an HTTPS testnet RPC endpoint");
  }
  if (!Number.isSafeInteger(since) || since < 0) fail("Transaction scan start time is invalid");
  if (!(await mnemonicValidate(mnemonic))) fail("APP_WALLET_MNEMONIC is invalid");

  const keyPair = await mnemonicToWalletKey(mnemonic);
  const wallet = WalletContractV5R1.create({
    workchain: 0,
    publicKey: keyPair.publicKey,
    walletId: {
      networkGlobalId: -3,
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
    fail("TON_DEPOSIT_ADDRESS does not match the testnet Wallet V5R1 derived from APP_WALLET_MNEMONIC");
  }

  const client = new TonClient({ endpoint, apiKey });
  const transfers = [];
  let lt;
  let hash;
  let reachedStart = false;

  for (let page = 0; page < 100; page += 1) {
    const transactions = await getTransactionsWithRetry(client, wallet.address, {
      limit: 100,
      ...(lt && hash ? { lt, hash } : {}),
    });
    if (!transactions.length) {
      reachedStart = true;
      break;
    }

    for (const transaction of transactions) {
      if (transaction.now < since - 60) {
        reachedStart = true;
        continue;
      }
      const message = transaction.inMessage;
      if (
        !message
        || message.info.type !== "internal"
        || !message.info.dest.equals(wallet.address)
      ) continue;
      const comment = readComment(message.body);
      if (!comment) continue;
      const bounceOutput = [...transaction.outMessages.values()].some(
        (output) => output.info.type === "internal" && output.info.bounced,
      );
      transfers.push({
        tx_hash: transaction.hash().toString("hex"),
        amount_nano: message.info.value.coins.toString(),
        comment,
        incoming_bounced: message.info.bounced,
        bounce_output: bounceOutput,
      });
    }

    const oldest = transactions.at(-1);
    if (oldest.now < since - 60) {
      reachedStart = true;
      break;
    }
    lt = oldest.lt.toString();
    hash = oldest.hash().toString("base64");
  }

  if (!reachedStart) fail("TON transaction history exceeded the configured scan window");
  process.stdout.write(JSON.stringify(transfers));
}

main().catch(() => fail("TON transaction scan failed"));
