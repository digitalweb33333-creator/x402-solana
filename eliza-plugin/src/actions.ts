/**
 * Builds one ElizaOS Action per catalogue endpoint.
 *
 * Each action:
 *  - is named after the endpoint (e.g. X402_GLEIF_LEI),
 *  - carries a dense, discovery-friendly description (+ price + usage prompt),
 *  - extracts call parameters from explicit options, structured content, or the
 *    natural-language message (via the runtime model), validating required fields,
 *  - calls the endpoint (auto-pay or discovery) and reports the result via callback.
 */

import type {
  Action,
  ActionExample,
  HandlerCallback,
  IAgentRuntime,
  Memory,
  State,
} from "@elizaos/core";

import { endpoints } from "./catalog.js";
import { callEndpoint, resolveConfig } from "./client.js";
import type { CatalogEndpoint } from "./types.js";

// ModelType.TEXT_SMALL is the string "TEXT_SMALL" in @elizaos/core. Using the literal
// keeps @elizaos/core a pure type-only dependency (no runtime import of the package).
const MODEL_TEXT_SMALL = "TEXT_SMALL";

function actionDescription(ep: CatalogEndpoint): string {
  const parts = [
    ep.description.trim(),
    `Price: ${ep.price} per call (x402 payment, USDC on Base mainnet).`,
  ];
  if (ep.llm_usage_prompt) parts.push(ep.llm_usage_prompt.trim());
  return parts.filter(Boolean).join(" ");
}

function buildSimiles(ep: CatalogEndpoint): string[] {
  const out = new Set<string>();
  const toolUpper = ep.tool.toUpperCase();
  out.add(toolUpper);
  out.add(`GET_${toolUpper}`);
  for (const tag of ep.tags || []) {
    out.add(tag.toUpperCase().replace(/[^A-Z0-9]+/g, "_"));
  }
  out.delete(ep.action); // never alias to itself
  return [...out];
}

function exampleQuery(ep: CatalogEndpoint): string {
  const required = ep.inputSchema.required || [];
  const props = ep.inputSchema.properties || {};
  const keys = required.length ? required : Object.keys(props).slice(0, 1);
  const hints = keys
    .map((k) => {
      const d = props[k]?.description || "";
      const m = d.match(/e\.g\.\s*'([^']+)'/i) || d.match(/e\.g\.\s*"([^"]+)"/i);
      return m ? `${k}=${m[1]}` : k;
    })
    .join(", ");
  return hints;
}

function buildExamples(ep: CatalogEndpoint): ActionExample[][] {
  const q = exampleQuery(ep);
  const userText = q
    ? `Use ${ep.tool} for ${q}`
    : `Use the ${ep.tool} tool`;
  return [
    [
      { name: "{{user}}", content: { text: userText } },
      {
        name: "{{agent}}",
        content: {
          text: `Calling the paid x402 tool ${ep.tool} (${ep.price}).`,
          actions: [ep.action],
        },
      },
    ],
  ];
}

function parseJsonObject(text: string): Record<string, unknown> | null {
  if (!text) return null;
  const start = text.indexOf("{");
  const end = text.lastIndexOf("}");
  if (start === -1 || end === -1 || end <= start) return null;
  try {
    const obj = JSON.parse(text.slice(start, end + 1));
    return obj && typeof obj === "object" ? (obj as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

function pickSchemaParams(
  source: unknown,
  ep: CatalogEndpoint,
): Record<string, unknown> {
  if (!source || typeof source !== "object") return {};
  const props = ep.inputSchema.properties || {};
  const out: Record<string, unknown> = {};
  for (const key of Object.keys(props)) {
    const v = (source as Record<string, unknown>)[key];
    if (v !== undefined && v !== null && v !== "") out[key] = v;
  }
  return out;
}

async function extractParams(
  runtime: IAgentRuntime,
  message: Memory,
  ep: CatalogEndpoint,
  options: unknown,
): Promise<Record<string, unknown>> {
  // 1. Explicit options (programmatic call): options.params or options itself.
  const optsObj = options as Record<string, unknown> | undefined;
  const fromOptions = pickSchemaParams(optsObj?.params ?? optsObj, ep);
  if (Object.keys(fromOptions).length) return fromOptions;

  // 2. Structured message content.
  const content = (message?.content ?? {}) as Record<string, unknown>;
  const fromContent = pickSchemaParams(content.params ?? content, ep);
  if (Object.keys(fromContent).length) return fromContent;

  // 3. LLM extraction from the natural-language text.
  const text = typeof content.text === "string" ? content.text : "";
  if (text && typeof runtime?.useModel === "function") {
    const props = ep.inputSchema.properties || {};
    const required = ep.inputSchema.required || [];
    const schemaDesc = Object.entries(props)
      .map(
        ([k, v]) =>
          `- ${k} (${v.type}${required.includes(k) ? ", required" : ""}): ${v.description || ""}`,
      )
      .join("\n");
    const prompt =
      `Extract the call parameters for the tool "${ep.tool}" from the user message. ` +
      `Respond with ONLY a JSON object of parameter values, no prose, no markdown.\n\n` +
      `Parameters:\n${schemaDesc}\n\nUser message: """${text}"""\n\nJSON:`;
    try {
      const out = await runtime.useModel(MODEL_TEXT_SMALL as never, { prompt });
      const raw = typeof out === "string" ? out : (out as any)?.text ?? "";
      const parsed = parseJsonObject(raw);
      if (parsed) return pickSchemaParams(parsed, ep);
    } catch {
      // model unavailable -> fall through (handler will report missing params)
    }
  }
  return {};
}

function formatData(ep: CatalogEndpoint, data: unknown): string {
  let body: string;
  try {
    body = JSON.stringify(data, null, 2);
  } catch {
    body = String(data);
  }
  if (body.length > 4000) body = body.slice(0, 4000) + "\n… (truncated)";
  return `${ep.tool} (paid x402 call, ${ep.price}) returned:\n${body}`;
}

// ActionResult.data is typed as Record<string, unknown>; payloads here are objects
// (or raw upstream JSON). This bridges the typing without changing runtime shape.
const asData = (x: unknown): Record<string, unknown> => x as Record<string, unknown>;

function buildAction(ep: CatalogEndpoint): Action {
  return {
    name: ep.action,
    similes: buildSimiles(ep),
    description: actionDescription(ep),
    examples: buildExamples(ep),
    // Available whenever the user intent maps here; missing params are handled in the handler.
    validate: async (_runtime: IAgentRuntime, _message: Memory) => true,
    handler: async (
      runtime: IAgentRuntime,
      message: Memory,
      _state?: State,
      options?: unknown,
      callback?: HandlerCallback,
    ) => {
      const cfg = resolveConfig((k) => runtime.getSetting?.(k));
      const params = await extractParams(runtime, message, ep, options);

      const required = ep.inputSchema.required || [];
      const missing = required.filter((r) => params[r] === undefined || params[r] === "");
      if (missing.length) {
        const text =
          `The ${ep.tool} tool needs ${missing.join(", ")} to run ` +
          `(${ep.description.split(".")[0]}.). Please provide ${missing.join(" and ")}.`;
        await callback?.({ text, actions: [ep.action], source: (message?.content as any)?.source });
        return { success: false, text, data: asData({ missing }), error: new Error(`missing params: ${missing.join(", ")}`) };
      }

      const result = await callEndpoint(ep, params, cfg);

      if (result.kind === "data") {
        const text = formatData(ep, result.data);
        await callback?.({ text, actions: [ep.action], source: (message?.content as any)?.source });
        // data is the raw upstream payload (object or array); cast for ActionResult typing.
        return { success: true, text, data: asData(result.data) };
      }
      if (result.kind === "payment_required") {
        const t = result.terms;
        const text =
          `${ep.tool} requires an x402 micropayment of ${t.price} (USDC on ${t.network}). ` +
          `Pay to ${t.payTo} via the x402 protocol, or configure X402_BUYER_PRIVATE_KEY to auto-pay. ` +
          `Example output:\n${JSON.stringify(t.exampleOutput, null, 2)}`;
        await callback?.({ text, actions: [ep.action], source: (message?.content as any)?.source });
        return { success: true, text, data: asData(t) };
      }
      const text = `Call to ${ep.tool} failed: ${result.message}${result.detail ? ` (${result.detail})` : ""}`;
      await callback?.({ text, actions: [ep.action], source: (message?.content as any)?.source });
      return { success: false, text, data: asData(result), error: new Error(result.message) };
    },
  };
}

export function buildActions(): Action[] {
  return endpoints.map(buildAction);
}
