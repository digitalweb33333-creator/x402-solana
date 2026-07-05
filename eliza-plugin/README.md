# plugin-x402-endpoints — ElizaOS plugin

Exposes the **28 paid endpoints** of the [x402-endpoints](https://x402-endpoints.onrender.com)
catalogue as **native ElizaOS actions**. An Eliza agent can discover and call them in
natural language; each call is billed per use via the **x402 protocol** (USDC on **Base mainnet**).

The catalogue covers:
- **Official EU / global registries** — GLEIF (LEI), VIES (EU VAT), Companies House (UK),
  INSEE Sirene (FR), BODACC, EUR-Lex, Légifrance, EPO patents, TED tenders, sanctions
  screening, CVE, FDA recalls / drug labels, IBAN, ECB FX…
- **Crypto pre-trade data** — token-safety, derivatives-radar, wallet-xray, dex-cex-spread,
  new-pairs, plus Polymarket odds and web search/extract.

The actions are generated from `src/catalog.json`, which is bundled at build time, so the
plugin never diverges from the catalogue it was built from.

## Two modes

| Mode | When | An action call does |
|---|---|---|
| **Auto-pay** | a funded Base buyer key is configured | pays the x402 micropayment and returns **live data** |
| **Discovery** (default) | no key | returns the exact **payment terms** (price, network, asset, `payTo`, resource) + an example output |

> ⚠️ Never put a buyer key on a shared/hosted agent. Auto-pay is for an agent that runs
> with its own wallet.

## Install

```bash
npm install plugin-x402-endpoints
# peer dependency:
npm install @elizaos/core
```

## Usage

```ts
import { AgentRuntime } from "@elizaos/core";
import x402Plugin from "plugin-x402-endpoints";

const runtime = new AgentRuntime({
  character: {
    name: "MyAgent",
    plugins: ["plugin-x402-endpoints"],
    settings: {
      secrets: {
        // optional — enables auto-pay (a Base wallet funded in USDC):
        X402_BUYER_PRIVATE_KEY: process.env.X402_BUYER_PRIVATE_KEY,
      },
    },
  },
  plugins: [x402Plugin],
});
```

The agent now has 28 actions (`X402_GLEIF_LEI`, `X402_VIES_VAT`, `X402_CRYPTO_TOKEN_SAFETY`,
`X402_CRYPTO_DERIVATIVES_RADAR`, `X402_CRYPTO_WALLET_XRAY`, …) and an `X402_CATALOG`
provider that lists every paid tool in context. Example prompts:

- *"Look up LEI 529900T8BM49AURSDO55"* → `X402_GLEIF_LEI`
- *"Is token 0x… on Base a honeypot?"* → `X402_CRYPTO_TOKEN_SAFETY`
- *"Validate IE VAT 6388047V"* → `X402_VIES_VAT`

Parameters are taken from explicit action options, structured message content, or extracted
from the natural-language message via the runtime model.

## Settings

| Setting | Default | Role |
|---|---|---|
| `X402_BUYER_PRIVATE_KEY` | — | Private key of a **Base wallet funded in USDC**; enables auto-pay. `EVM_PRIVATE_KEY` / `WALLET_PRIVATE_KEY` are also accepted. |
| `X402_AUTO_PAY` | `1` | Set to `0`/`false` to force discovery mode even with a key. |
| `X402_BASE_URL` | `https://x402-endpoints.onrender.com` | API base URL. |
| `X402_NETWORK` | `eip155:8453` | Payment network (Base mainnet). |

## Build & test

```bash
npm install
npm run build     # tsup -> dist/ (ESM + CJS + d.ts)
npm run smoke     # discovery smoke test against the live API
```

## License

MIT
