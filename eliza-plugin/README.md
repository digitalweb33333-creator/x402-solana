# plugin-x402-solana — ElizaOS plugin

Exposes the **10 paid endpoints** of the [x402-solana](https://x402-solana-cva8.onrender.com)
catalogue as **native ElizaOS actions**. An Eliza agent can discover and call them in
natural language; each call is billed per use via the **x402 protocol** (USDC on **Solana mainnet**,
settled through the Coinbase CDP facilitator — gasless for the buyer).

The catalogue covers:
- **Crypto & Solana pre-trade safety** — SPL/EVM token-safety (rug & honeypot checks),
  GO/NO-GO pre-trade verdicts, full token dossiers.
- **Market data** — Polymarket odds.
- **Official KYB / AML verification** — GLEIF (LEI) lookup, EU sanctions screening.
- **x402 discoverability** — agent rank check and visibility audit.

The actions are generated from `src/catalog.json`, which is bundled at build time, so the
plugin never diverges from the live catalogue it was built from.

## Mode

**Discovery (default).** An action call returns the exact x402 **payment terms** (price,
network, asset, `payTo`, resource) plus an example output. Pay the returned terms with any
x402-aware **Solana** client to receive live data.

> Solana auto-pay is not wired in this build — leave `X402_AUTO_PAY` at `0`.

## Install

```bash
npm install plugin-x402-solana
# peer dependency:
npm install @elizaos/core
```

## Usage

```ts
import { AgentRuntime } from "@elizaos/core";
import x402Plugin from "plugin-x402-solana";

const runtime = new AgentRuntime({
  character: {
    name: "MyAgent",
    plugins: ["plugin-x402-solana"],
    settings: {
      secrets: {
        // all optional — sensible Solana defaults are bundled:
        // X402_BASE_URL: "https://x402-solana-cva8.onrender.com",
      },
    },
  },
  plugins: [x402Plugin],
});
```

The agent now has 10 actions (`X402_GLEIF_LEI`, `X402_SANCTIONS_SCREEN`,
`X402_POLYMARKET_ODDS`, `X402_CRYPTO_TOKEN_SAFETY`, `X402_CRYPTO_PRE_TRADE_VERDICT`,
`X402_CRYPTO_TOKEN_DOSSIER`, `X402_SOLANA_TOKEN_SAFETY`, `X402_SOLANA_PRE_TRADE`,
`X402_AGENT_RANK_CHECK`, `X402_AGENT_VISIBILITY_AUDIT`) and an `X402_CATALOG` provider
that lists every paid tool in context. Example prompts:

- *"Look up LEI 529900T8BM49AURSDO55"* → `X402_GLEIF_LEI`
- *"Is this SPL token a honeypot?"* → `X402_SOLANA_TOKEN_SAFETY`
- *"Should I buy this token — GO or NO-GO?"* → `X402_SOLANA_PRE_TRADE`

Parameters are taken from explicit action options, structured message content, or extracted
from the natural-language message via the runtime model.

## Settings

| Setting | Default | Role |
|---|---|---|
| `X402_BASE_URL` | `https://x402-solana-cva8.onrender.com` | API base URL. |
| `X402_NETWORK` | `solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp` | Payment network (Solana mainnet). |
| `X402_AUTO_PAY` | `0` | Discovery mode. Solana auto-pay is not wired in this build; leave at `0`. |

## Build & test

```bash
npm install
npm run build     # tsup -> dist/ (ESM + CJS + d.ts)
npm run smoke     # discovery smoke test against the live API
```

## License

MIT
