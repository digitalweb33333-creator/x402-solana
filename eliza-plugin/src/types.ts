/**
 * Types describing the x402 catalogue consumed by the plugin.
 *
 * The catalogue (src/catalog.json) is the single source of truth: one entry per
 * deployed x402 endpoint. The plugin turns each entry into an ElizaOS Action and
 * lists them in a Provider, so an Eliza agent discovers and calls them natively.
 */

export interface CatalogInputSchema {
  type: "object";
  properties: Record<string, {
    type: string;
    description?: string;
    pattern?: string;
  }>;
  required?: string[];
}

export interface CatalogEndpoint {
  /** Short tool id, e.g. "gleif_lei". */
  tool: string;
  /** ElizaOS action name, e.g. "X402_GLEIF_LEI". */
  action: string;
  /** API path, e.g. "/gleif/lei". */
  path: string;
  /** HTTP method (all GET today). */
  method: string;
  /** Human price label, e.g. "$0.01". */
  price: string;
  /** Authority-rich semantic description (drives discovery). */
  description: string;
  /** Dense usage prompt for LLM agents. */
  llm_usage_prompt?: string;
  /** Semantic tags. */
  tags?: string[];
  /** JSON Schema for the query parameters. */
  inputSchema: CatalogInputSchema;
  /** A real example response (helps the agent before paying). */
  outputExample?: unknown;
  /** Payment metadata (mirrors the global values). */
  payTo?: string;
  network?: string;
  asset?: string;
}

export interface Catalog {
  name: string;
  baseUrl: string;
  network: string;
  asset: string;
  payTo: string;
  facilitator: string;
  endpoints: CatalogEndpoint[];
}

/** Result of a paid (or discovery) call to an endpoint. */
export type CallResult =
  | { kind: "data"; status: number; data: unknown }
  | { kind: "payment_required"; terms: PaymentTerms }
  | { kind: "error"; status?: number; message: string; detail?: string };

export interface PaymentTerms {
  x402PaymentRequired: true;
  tool: string;
  resource: string;
  price: string;
  network: string;
  asset?: string;
  payTo?: string;
  scheme: string;
  maxTimeoutSeconds: number;
  howToPay: string;
  exampleOutput?: unknown;
}
