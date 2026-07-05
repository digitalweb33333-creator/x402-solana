// Dernière tentative settle /agent/clearance via le client x402 TIERS (Coinbase x402-fetch,
// chemin X-PAYMENT standard — différent du PAYMENT-SIGNATURE du SDK Python).
import { wrapFetchWithPayment, decodeXPaymentResponse } from "x402-fetch";
import { createWalletClient, http } from "viem";
import { base } from "viem/chains";
import { privateKeyToAccount } from "viem/accounts";

let pk = process.env.PK || "";
if (pk && !pk.startsWith("0x")) pk = "0x" + pk;
const account = privateKeyToAccount(pk);
const wallet = createWalletClient({ account, chain: base, transport: http() });

const url = "https://x402-endpoints.onrender.com/agent/clearance";
const body = JSON.stringify({ action: "read a public registry entry", reversible: true });

console.log("buyer:", account.address, "-> x402-fetch (X-PAYMENT) -> /agent/clearance");

const tryOnce = async (maxValue) => {
  const fetchWithPayment = wrapFetchWithPayment(fetch, wallet, maxValue);
  const resp = await fetchWithPayment(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body,
  });
  console.log("status:", resp.status);
  const xpr = resp.headers.get("x-payment-response");
  if (xpr) {
    try { console.log("SETTLE:", JSON.stringify(decodeXPaymentResponse(xpr))); }
    catch (e) { console.log("x-payment-response (raw):", xpr.slice(0, 200)); }
  }
  const txt = await resp.text();
  console.log("body:", txt.slice(0, 400));
  return resp.status;
};

try {
  const st = await tryOnce(1000000n); // cap 1 USDC (6 decimals)
  process.exit(st === 200 ? 0 : 2);
} catch (e) {
  console.log("ERROR:", e?.name, e?.message);
  if (e?.cause) console.log("cause:", String(e.cause).slice(0, 300));
  process.exit(3);
}
