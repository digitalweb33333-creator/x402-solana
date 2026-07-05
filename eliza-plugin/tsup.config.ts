import { defineConfig } from "tsup";

export default defineConfig({
  entry: ["src/index.ts"],
  format: ["esm", "cjs"],
  dts: true,
  sourcemap: true,
  clean: true,
  treeshake: true,
  // The host Eliza runtime provides @elizaos/core; keep heavy deps external.
  external: ["@elizaos/core", "viem", "x402-fetch"],
  // catalog.json is bundled into the output (loader handles JSON).
  loader: { ".json": "json" },
});
