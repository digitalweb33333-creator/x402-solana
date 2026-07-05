/**
 * Smoke test for the built plugin (dist/). Runs in discovery mode (no wallet):
 *  1. the plugin exposes one action per catalogue endpoint,
 *  2. the catalogue provider renders,
 *  3. an action handler performs a real discovery call to the live API and returns
 *     the x402 payment terms.
 *
 * Usage: npm run build && npm run smoke
 */

import plugin, { endpoints, ENDPOINT_COUNT } from "../dist/index.js";

let failures = 0;
function check(label, cond) {
  const ok = !!cond;
  console.log(`${ok ? "PASS" : "FAIL"}  ${label}`);
  if (!ok) failures++;
}

// Minimal fake runtime: discovery mode (no buyer key), no model needed for option-based params.
const runtime = {
  getSetting: () => undefined,
  useModel: async () => "{}",
};

async function main() {
  console.log(`Plugin: ${plugin.name} — ${plugin.actions.length} actions, ${plugin.providers.length} provider(s)\n`);

  check(`exposes ${ENDPOINT_COUNT} actions (one per endpoint)`, plugin.actions.length === ENDPOINT_COUNT);
  check("ENDPOINT_COUNT matches catalogue", endpoints.length === ENDPOINT_COUNT);

  const names = new Set(plugin.actions.map((a) => a.name));
  check("action names are unique", names.size === plugin.actions.length);
  check("includes X402_GLEIF_LEI", names.has("X402_GLEIF_LEI"));
  check("includes X402_CRYPTO_TOKEN_SAFETY", names.has("X402_CRYPTO_TOKEN_SAFETY"));

  const gleif = plugin.actions.find((a) => a.name === "X402_GLEIF_LEI");
  check("GLEIF action has description with price", /\$0\.01/.test(gleif.description));
  check("GLEIF action has examples", Array.isArray(gleif.examples) && gleif.examples.length > 0);
  check("GLEIF validate() resolves true", (await gleif.validate(runtime, { content: { text: "" } })) === true);

  // Provider renders the catalogue.
  const provider = plugin.providers[0];
  const pr = await provider.get(runtime, { content: { text: "" } }, {});
  check("provider returns catalogue text", typeof pr.text === "string" && pr.text.includes("x402 paid tools"));
  check("provider reports correct count", pr.values?.x402_tool_count === ENDPOINT_COUNT);
  check("provider reports discovery mode (no wallet)", pr.values?.x402_mode === "discovery");

  // Missing-params path.
  let cbText = "";
  const cb = async (c) => { cbText = c.text; return []; };
  const miss = await gleif.handler(runtime, { content: { text: "" } }, {}, {}, cb);
  check("missing required param -> success:false", miss.success === false);
  check("missing param message mentions 'lei'", /lei/i.test(cbText));

  // Real discovery call to the live API (expects 402 -> payment terms).
  cbText = "";
  const res = await gleif.handler(
    runtime,
    { content: { text: "look up LEI 529900T8BM49AURSDO55" } },
    {},
    { params: { lei: "529900T8BM49AURSDO55" } },
    cb,
  );
  console.log("\nLive discovery result:", JSON.stringify(res.data, null, 2)?.slice(0, 600), "\n");
  const data = res.data || {};
  const gotTerms = data.x402PaymentRequired === true || data.lei !== undefined;
  check("live call returned terms (discovery) or data (if funded)", res.success === true && gotTerms);
  if (data.x402PaymentRequired) {
    check("payment terms include price $0.01", data.price === "$0.01");
    check(
      "payment terms include Solana payTo",
      data.payTo === "CucGfdmABDC3QvaZdn9AwUfYBCmmvYjTDdq3WBHXDLEF",
    );
  }

  console.log(`\n${failures === 0 ? "ALL CHECKS PASSED" : failures + " CHECK(S) FAILED"}`);
  // Set exit code but let the event loop drain (avoids a libuv teardown assertion on
  // Windows when forcing exit while keep-alive sockets are still closing).
  process.exitCode = failures === 0 ? 0 : 1;
}

main().catch((e) => {
  console.error("Smoke test crashed:", e);
  process.exitCode = 1;
});
