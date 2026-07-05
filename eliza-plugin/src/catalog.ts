/**
 * Loads and exposes the bundled x402 catalogue.
 *
 * catalog.json is embedded at build time (resolveJsonModule), so the published
 * plugin is self-contained and never diverges from the deployed catalogue it was
 * built from. Runtime settings can still override baseUrl/network if needed.
 */

import rawCatalog from "./catalog.json";
import type { Catalog, CatalogEndpoint } from "./types.js";

export const catalog: Catalog = rawCatalog as Catalog;

export const endpoints: CatalogEndpoint[] = catalog.endpoints;

/** Map action name -> endpoint, for O(1) handler dispatch. */
export const endpointByAction: Record<string, CatalogEndpoint> = Object.fromEntries(
  endpoints.map((e) => [e.action, e]),
);

/** Guardrail: unique action names (duplicate names would shadow each other). */
const seen = new Set<string>();
for (const e of endpoints) {
  if (seen.has(e.action)) {
    throw new Error(`Duplicate x402 action name: ${e.action} (${e.path})`);
  }
  seen.add(e.action);
}

export const ENDPOINT_COUNT = endpoints.length;
