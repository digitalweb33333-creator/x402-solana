/**
 * plugin-x402-solana
 *
 * ElizaOS plugin exposing the x402-solana catalogue (10 paid endpoints:
 * crypto/Solana pre-trade safety, market data, KYB/AML verification and x402
 * discoverability) as native Eliza actions, billed per call via the x402 protocol
 * (USDC on Solana mainnet).
 *
 * Discovery mode by default (actions return the exact x402 payment terms). Solana
 * auto-pay is not wired in this build; pay the returned terms with any x402-aware
 * Solana client.
 */

import type { Plugin } from "@elizaos/core";

import { buildActions } from "./actions.js";
import { x402CatalogProvider } from "./provider.js";
import { catalog, endpoints, ENDPOINT_COUNT } from "./catalog.js";

export const x402Plugin: Plugin = {
  name: "x402-solana",
  description:
    `Exposes the ${ENDPOINT_COUNT} paid x402-solana data tools (crypto & Solana ` +
    `pre-trade safety: token-safety, pre-trade verdict, token dossier; market data: ` +
    `Polymarket odds; KYB/AML: GLEIF LEI, sanctions screening; x402 discoverability: ` +
    `agent rank & visibility audit) as native ElizaOS actions, billed per call via ` +
    `x402 (USDC on Solana mainnet). Actions return exact payment terms for discovery; ` +
    `pay them with any x402-aware Solana client to receive live data.`,
  actions: buildActions(),
  providers: [x402CatalogProvider],
};

export default x402Plugin;

export { catalog, endpoints, ENDPOINT_COUNT } from "./catalog.js";
export { x402CatalogProvider } from "./provider.js";
export { buildActions } from "./actions.js";
export { callEndpoint, resolveConfig } from "./client.js";
export * from "./types.js";
