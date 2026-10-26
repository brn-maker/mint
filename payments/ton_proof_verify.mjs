import { Address } from "@ton/core";
import { sha256, signVerify } from "@ton/crypto";
import { TonClient, WalletContractV4, WalletContractV5R1 } from "@ton/ton";
import { stdin } from "node:process";

const PROOF_PREFIX = "ton-proof-item-v2/";
const CONNECT_PREFIX = "ton-connect";
const VALID_AUTH_TIME_SECONDS = 15 * 60; // 15 minutes

function fail(message) {
  process.stdout.write(JSON.stringify({ valid: false, error: message }));
  process.exit(0);
}

async function readInput() {
  let input = "";
  for await (const chunk of stdin) input += chunk;
  return JSON.parse(input);
}

function deriveAddresses(publicKey, workchain, networkGlobalId) {
  // Try the standard wallet versions: V5R1 (modern wallets incl. Telegram) and V4R2.
  const v5r1 = WalletContractV5R1.create({
    workchain,
    publicKey,
    walletId: {
      networkGlobalId,
      context: { walletVersion: "v5r1", workchain, subwalletNumber: 0 },
    },
  });
  const v4 = WalletContractV4.create({ workchain, publicKey });
  return [v5r1.address, v4.address];
}

async function getOnChainPublicKey(address) {
  const endpoint = process.env.TON_API_ENDPOINT;
  const apiKey = process.env.TON_API_KEY;
  if (!endpoint || !apiKey) {
    throw new Error("TON RPC is not configured");
  }
  const client = new TonClient({ endpoint, apiKey, timeout: 15000 });
  let lastError;
  for (let attempt = 0; attempt < 5; attempt += 1) {
    try {
      const result = await client.runMethod(address, "get_public_key");
      const key = result.stack.readBigNumber();
      return key.toString(16).padStart(64, "0");
    } catch (error) {
      lastError = error;
      await new Promise((resolve) => setTimeout(resolve, 750 * 2 ** attempt));
    }
  }
  throw lastError;
}

async function main() {
  const input = await readInput();
  const addressStr = input?.address;
  const publicKeyHex = input?.public_key;
  const proof = input?.proof;
  const expectedDomain = input?.expected_domain;
  if (!addressStr || !publicKeyHex || !proof) {
    fail("Missing address, public_key or proof");
  }

  let address;
  try {
    address = Address.parse(addressStr);
  } catch {
    fail("Invalid wallet address");
  }

  // Domain checks
  const domainValue = proof.domain?.value || "";
  const domainLengthBytes = proof.domain?.lengthBytes;
  const domainBytes = Buffer.from(domainValue, "utf8");
  if (expectedDomain && domainValue !== expectedDomain) {
    fail("Proof domain does not match the app domain");
  }
  if (typeof domainLengthBytes !== "number" || domainLengthBytes !== domainBytes.length) {
    fail("Proof domain length does not match its byte length");
  }

  // Timestamp freshness
  const timestamp = Number(proof.timestamp);
  const now = Math.floor(Date.now() / 1000);
  if (!Number.isFinite(timestamp) || Math.abs(now - timestamp) > VALID_AUTH_TIME_SECONDS) {
    fail("Proof timestamp is not fresh");
  }

  // Assemble the exact message the wallet signed.
  // message = "ton-proof-item-v2/" ++ workchain(int32 BE) ++ address(32) ++
  //           domainLength(uint32 LE) ++ domain ++ timestamp(uint64 LE) ++ payload
  const workchain = Buffer.alloc(4);
  workchain.writeInt32BE(address.workChain, 0);
  const domainLength = Buffer.alloc(4);
  domainLength.writeUInt32LE(domainLengthBytes, 0);
  const timestampBytes = Buffer.alloc(8);
  timestampBytes.writeBigUInt64LE(BigInt(timestamp), 0);

  const message = Buffer.concat([
    Buffer.from(PROOF_PREFIX, "utf8"),
    workchain,
    Buffer.from(address.hash),
    domainLength,
    domainBytes,
    timestampBytes,
    Buffer.from(String(proof.payload ?? ""), "utf8"),
  ]);

  // digest = sha256(0xffff ++ "ton-connect" ++ sha256(message))
  const messageHash = Buffer.from(await sha256(message));
  const fullMessage = Buffer.concat([
    Buffer.from([0xff, 0xff]),
    Buffer.from(CONNECT_PREFIX, "utf8"),
    messageHash,
  ]);
  const digest = Buffer.from(await sha256(fullMessage));

  // Verify the signature with the provided public key.
  let publicKey;
  try {
    publicKey = Buffer.from(publicKeyHex, "hex");
  } catch {
    fail("Invalid public key encoding");
  }
  if (publicKey.length !== 32) {
    fail("Public key must be 32 bytes");
  }
  let signature;
  try {
    signature = Buffer.from(proof.signature, "base64");
  } catch {
    fail("Invalid signature encoding");
  }
  if (!(await signVerify(digest, signature, publicKey))) {
    fail("Invalid proof signature");
  }

  // Bind the public key to the address. Prefer deriving the address from the
  // public key (works even for undeployed wallets); fall back to the on-chain
  // get_public_key method for non-standard wallet versions.
  const network = input?.network || "-239";
  const networkGlobalId = Number(network);
  let keyMatchesAddress = deriveAddresses(publicKey, address.workChain, networkGlobalId)
    .some((candidate) => candidate.equals(address));
  if (!keyMatchesAddress) {
    try {
      const onChainKey = await getOnChainPublicKey(address);
      keyMatchesAddress = onChainKey.toLowerCase() === publicKeyHex.toLowerCase();
    } catch {
      keyMatchesAddress = false;
    }
  }
  if (!keyMatchesAddress) {
    fail("Public key does not match the wallet address");
  }

  process.stdout.write(JSON.stringify({
    valid: true,
    address: address.toRawString(),
    domain: domainValue,
  }));
}

main().catch((error) => fail(`Verification failed: ${error?.message || error}`));
