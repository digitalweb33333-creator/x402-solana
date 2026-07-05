# x402-solana

Paid **x402** API tools for AI agents, settled in **USDC on Solana** (mainnet). A Solana-rail
replica of [x402-endpoints](https://github.com/digitalweb33333-creator/x402-endpoints) (Base): same
FastAPI quality, same CDP facilitator — payments routed on Solana instead of Base.

- **No API key** — payment is authentication.
- **Gasless for the buyer** — the CDP facilitator is the transaction fee payer.
- **USDC mint** `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v` (6 decimals), network `solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp`.

## Endpoints (10)

| Path | Price | What it does |
|---|---|---|
| `GET /gleif/lei` | $0.01 | GLEIF LEI lookup (KYB, official global registry) |
| `GET /sanctions/screen` | $0.05 | EU consolidated sanctions / AML screening |
| `GET /polymarket/odds` | $0.05 | Live Polymarket prediction-market odds |
| `GET /crypto/token-safety` | $0.05 | EVM token honeypot / rug / tax safety score |
| `GET /crypto/pre-trade-verdict` | $0.05 | Fused GO/CAUTION/NO-GO pre-trade verdict (signed) |
| `GET /crypto/token-dossier` | $0.10 | Deep token dossier: holders, liquidity, red flags |
| `GET /solana/token-safety` | $0.01 | Solana SPL rug/honeypot (static + behavioral) |
| `GET /solana/pre-trade` | $0.05 | All-in-one Solana pre-trade BUY-SAFE/CAUTION/AVOID |
| `GET /agent/rank-check` | $0.10 | x402 seller keyword-rank pulse in CDP Bazaar |
| `GET /agent/visibility-audit` | $1.00 | Full x402 discoverability audit (Ed25519-signed) |

Discovery: `/.well-known/x402.json`, `/.well-known/agent-card.json`, `/llms.txt`. Health: `/health`.

## Run locally

```bash
python3.12 -m venv .venv
./.venv/bin/pip install -r requirements.txt
# set env (see .env.example) — CDP keys required for the facilitator
./.venv/bin/uvicorn app.main:app --port 8000
```

## Deploy (Render)

One-click via `render.yaml` (Blueprint). Set the `sync:false` secrets (`CDP_API_KEY_ID`,
`CDP_API_KEY_SECRET`, `RECEIPT_SIGNING_SEED`, optional `ANTHROPIC_API_KEY`) in the dashboard.

## Architecture

Payment layer is 100% in `app/main.py` (`register_exact_svm_server` + `PaymentOption(network=solana…)`);
endpoint handlers in `app/routers/` are payment-agnostic. Description ≤ 500 chars is enforced at
startup (hard CDP `/verify` constraint). See `DIFFERENCES-BASE-SOLANA.md` for the full Base→Solana map.

## Tooling (`tools/`)

- `check_facilitator.py` — verify the CDP facilitator advertises Solana support.
- `lint_descriptions.py` — English-only + ≤500-char lint.
- `gen_discovery.py` — regenerate the discovery files from the routes.
- `create_seller_ata.py` — create the seller USDC ATA (settlement prerequisite).
- `settle.py` — buyer→endpoint settlement (dry by default, `--execute` to pay).
