/**
 * x402 HTTP client for the plugin.
 *
 * Two modes, mirroring the MCP server:
 *  - auto-pay  : a funded Base buyer key is configured -> pays the 402 and returns live data.
 *  - discovery : no key (or auto-pay disabled) -> returns the exact payment terms.
 *
 * x402-fetch + viem are imported lazily so an agent that never auto-pays does not
 * need a signer at runtime.
 */

import { catalog } from "./catalog.js";
import type { CatalogEndpoint, CallResult, PaymentTerms } from "./types.js";

export interface ClientConfig {
  baseUrl: string;
  network: string;
  buyerPrivateKey?: string;
  autoPay: boolean;
}

// Broad return type: runtime.getSetting may yield string | number | boolean | undefined.
type GetSetting = (key: string) => unknown;

/** Resolve runtime config from agent settings, falling back to the bundled catalogue. */
export function resolveConfig(getSetting: GetSetting): ClientConfig {
  const get = (k: string) => {
    const v = getSetting(k);
    return v == null ? undefined : String(v).trim() || undefined;
  };
  const baseUrl = (get("X402_BASE_URL") || catalog.baseUrl).replace(/\/+$/, "");
  const network = get("X402_NETWORK") || catalog.network;
  const buyerPrivateKey =
    get("X402_BUYER_PRIVATE_KEY") ||
    get("EVM_PRIVATE_KEY") ||
    get("WALLET_PRIVATE_KEY") ||
    undefined;
  const autoPayFlag = (get("X402_AUTO_PAY") || "1").toLowerCase();
  const autoPay = !!buyerPrivateKey && !["0", "false", "no", "off"].includes(autoPayFlag);
  return { baseUrl, network, buyerPrivateKey, autoPay };
}

// Cache the wrapped fetch per buyer key (building a signer is not free).
let _paidFetch: typeof fetch | null = null;
let _paidFetchKey: string | null = null;

async function getPaidFetch(cfg: ClientConfig): Promise<typeof fetch | null> {
  if (!cfg.autoPay || !cfg.buyerPrivateKey) return null;
  if (_paidFetch && _paidFetchKey === cfg.buyerPrivateKey) return _paidFetch;
  const { wrapFetchWithPayment } = await import("x402-fetch");
  const { privateKeyToAccount } = await import("viem/accounts");
  const raw = cfg.buyerPrivateKey;
  const pk = (raw.startsWith("0x") ? raw : `0x${raw}`) as `0x${string}`;
  const account = privateKeyToAccount(pk);
  _paidFetch = wrapFetchWithPayment(fetch, account as never) as unknown as typeof fetch;
  _paidFetchKey = cfg.buyerPrivateKey;
  return _paidFetch;
}

function buildUrl(baseUrl: string, path: string, params: Record<string, unknown>): string {
  const url = new URL(baseUrl + path);
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "") continue;
    url.searchParams.set(k, String(v));
  }
  return url.toString();
}

function decodeBase64Json(value: string): unknown | null {
  try {
    const json =
      typeof Buffer !== "undefined"
        ? Buffer.from(value, "base64").toString("utf-8")
        : atob(value);
    return JSON.parse(json);
  } catch {
    return null;
  }
}

function decodePaymentRequired(header: string | null, body: unknown): any | null {
  if (header) {
    const decoded = decodeBase64Json(header);
    if (decoded) return decoded;
  }
  if (body && typeof body === "object" && ("accepts" in (body as object) || "x402Version" in (body as object))) {
    return body;
  }
  return null;
}

function toPaymentTerms(ep: CatalogEndpoint, decoded: any, cfg: ClientConfig): PaymentTerms {
  const accepts: any[] = (decoded?.accepts as any[]) || [];
  const a = accepts[0] || {};
  return {
    x402PaymentRequired: true,
    tool: ep.tool,
    resource: cfg.baseUrl + ep.path,
    price: ep.price,
    network: a.network || ep.network || cfg.network,
    asset: a.asset || ep.asset,
    payTo: a.payTo || a.pay_to || ep.payTo || catalog.payTo,
    scheme: a.scheme || "exact",
    maxTimeoutSeconds: a.maxTimeoutSeconds || a.max_timeout_seconds || 300,
    howToPay:
      "This endpoint requires an x402 micropayment (USDC on Base mainnet). Configure " +
      "X402_BUYER_PRIVATE_KEY (a funded Base wallet) on this agent to auto-pay and receive " +
      "live data, or complete the payment with any x402-aware HTTP client.",
    exampleOutput: ep.outputExample,
  };
}

async function readBody(resp: Response): Promise<unknown> {
  const text = await resp.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

/** Call one endpoint. Auto-pays when configured and funded; otherwise returns terms. */
export async function callEndpoint(
  ep: CatalogEndpoint,
  params: Record<string, unknown>,
  cfg: ClientConfig,
): Promise<CallResult> {
  // Method-aware: GET endpoints carry params in the query string; POST endpoints
  // (the /agent/* decision endpoints) send a JSON body.
  const method = (ep.method || "GET").toUpperCase();
  const isPost = method === "POST";
  const url = isPost ? `${cfg.baseUrl}${ep.path}` : buildUrl(cfg.baseUrl, ep.path, params);
  const headers: Record<string, string> = isPost
    ? { accept: "application/json", "content-type": "application/json" }
    : { accept: "application/json" };
  const reqInit: RequestInit = isPost
    ? { method, headers, body: JSON.stringify(params || {}) }
    : { method, headers };

  const paidFetch = await getPaidFetch(cfg).catch(() => null);

  if (paidFetch) {
    try {
      const resp = await paidFetch(url, reqInit);
      const body = await readBody(resp);
      if (resp.status === 200) return { kind: "data", status: 200, data: body };
      if (resp.status === 402) {
        const decoded = decodePaymentRequired(resp.headers.get("payment-required"), body);
        return { kind: "payment_required", terms: toPaymentTerms(ep, decoded, cfg) };
      }
      return {
        kind: "error",
        status: resp.status,
        message: `upstream returned HTTP ${resp.status}`,
        detail: typeof body === "string" ? body.slice(0, 500) : JSON.stringify(body).slice(0, 500),
      };
    } catch (err: any) {
      // Payment failed (e.g. unfunded wallet): fall back to a discovery call for terms.
      // continue below
    }
  }

  let resp: Response;
  try {
    resp = await fetch(url, reqInit);
  } catch (err: any) {
    return { kind: "error", message: "request failed", detail: `${err?.name}: ${err?.message}` };
  }
  const body = await readBody(resp);
  if (resp.status === 200) return { kind: "data", status: 200, data: body };
  if (resp.status === 402) {
    const decoded = decodePaymentRequired(resp.headers.get("payment-required"), body);
    return { kind: "payment_required", terms: toPaymentTerms(ep, decoded, cfg) };
  }
  return {
    kind: "error",
    status: resp.status,
    message: `unexpected HTTP ${resp.status}`,
    detail: typeof body === "string" ? body.slice(0, 500) : JSON.stringify(body).slice(0, 500),
  };
}
