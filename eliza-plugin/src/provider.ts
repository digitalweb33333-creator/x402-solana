/**
 * Provider that injects the x402 catalogue into the agent's context, so the agent
 * is aware of every paid tool it can call (drives tool selection / discovery).
 */

import type { IAgentRuntime, Memory, Provider, ProviderResult, State } from "@elizaos/core";

import { endpoints } from "./catalog.js";
import { resolveConfig } from "./client.js";

function firstSentence(s: string): string {
  const m = s.match(/^.*?[.!?](\s|$)/);
  return (m ? m[0] : s).trim();
}

export const x402CatalogProvider: Provider = {
  name: "X402_CATALOG",
  description:
    "Catalogue of paid x402 data tools (official EU/global registries + crypto pre-trade data) callable by this agent.",
  // Not dynamic: always surface the catalogue so the agent can pick a paid tool.
  dynamic: false,
  get: async (
    runtime: IAgentRuntime,
    _message: Memory,
    _state: State,
  ): Promise<ProviderResult> => {
    const cfg = resolveConfig((k) => runtime.getSetting?.(k));
    const mode = cfg.autoPay
      ? "auto-pay (a funded buyer wallet is configured; calls return live data)"
      : "discovery (no buyer wallet; calls return payment terms until X402_BUYER_PRIVATE_KEY is set)";
    const lines = endpoints.map(
      (e) => `- ${e.action} (${e.price}): ${firstSentence(e.description)}`,
    );
    const text =
      `# x402 paid tools available (${endpoints.length})\n` +
      `Billed per call via the x402 protocol (USDC on Base mainnet). Mode: ${mode}.\n` +
      lines.join("\n");
    return {
      text,
      values: { x402_tool_count: endpoints.length, x402_mode: cfg.autoPay ? "auto-pay" : "discovery" },
      data: { baseUrl: cfg.baseUrl, network: cfg.network, count: endpoints.length },
    };
  },
};
