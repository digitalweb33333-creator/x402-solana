/**
 * @x402-endpoints/plugin-elizaos
 *
 * ElizaOS plugin exposing the x402-endpoints catalogue (28 paid data endpoints:
 * official EU/global registries + crypto pre-trade data) as native Eliza actions,
 * billed per call via the x402 protocol (USDC on Base mainnet).
 *
 * Configure X402_BUYER_PRIVATE_KEY (a funded Base wallet) to auto-pay and receive
 * live data; otherwise actions return the exact payment terms (discovery).
 */

import type { Plugin } from "@elizaos/core";

import { buildActions } from "./actions.js";
import { x402CatalogProvider } from "./provider.js";
import { catalog, endpoints, ENDPOINT_COUNT } from "./catalog.js";

export const x402Plugin: Plugin = {
  name: "x402-endpoints",
  description:
    `Exposes the ${ENDPOINT_COUNT} paid x402-endpoints data tools (official EU/global ` +
    `registries: GLEIF, VIES, BODACC, EUR-Lex, Companies House, EPO, sanctions, CVE, FDA… ` +
    `+ crypto pre-trade data: token-safety, derivatives-radar, wallet-xray, dex-cex-spread) ` +
    `as native ElizaOS actions, billed per call via x402 (USDC on Base mainnet). ` +
    `Set X402_BUYER_PRIVATE_KEY to auto-pay and receive live data; otherwise actions ` +
    `return exact payment terms for discovery.`,
  actions: buildActions(),
  providers: [x402CatalogProvider],
};

export default x402Plugin;

export { catalog, endpoints, ENDPOINT_COUNT } from "./catalog.js";
export { x402CatalogProvider } from "./provider.js";
export { buildActions } from "./actions.js";
export { callEndpoint, resolveConfig } from "./client.js";
export * from "./types.js";
