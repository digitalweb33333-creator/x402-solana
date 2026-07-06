"""Application FastAPI x402-solana — seller x402 sur rail Solana (USDC SPL).

Réplique la qualité du projet Base (~/x402-endpoints) mais route les paiements sur
Solana mainnet via le MÊME facilitator CDP (qui supporte Solana nativement).

Différence unique vs Base au niveau paiement :
- `register_exact_svm_server` (au lieu de `register_exact_evm_server`)
- `NETWORK = solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp` (au lieu de eip155:8453)
- `pay_to = SOLANA_SELLER_ADDRESS` (base58 au lieu de 0x)

Les handlers (app/routers/*.py) sont agnostiques du réseau : copiés verbatim de Base.
Le settlement Solana est un transfert SPL USDC (ATA→ATA) où le facilitator CDP est
feePayer (gasless côté buyer). La borne DURE des 500 caractères de description
(contrainte /verify du facilitator CDP) s'applique aussi ici — guard fail-fast.
"""

import contextlib
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from starlette.exceptions import HTTPException
from starlette.routing import Mount

from x402 import x402ResourceServer
from x402.extensions.bazaar import (
    OutputConfig,
    bazaar_resource_server_extension,
    declare_discovery_extension,
)
from x402.http import (
    CreateHeadersAuthProvider,
    FacilitatorConfig,
    HTTPFacilitatorClient,
    PAYMENT_RESPONSE_HEADER,
    decode_payment_response_header,
)
from x402.http.middleware.fastapi import payment_middleware
from x402.http.types import PaymentOption, RouteConfig
from x402.mechanisms.svm.exact import register_exact_svm_server

from app.cdp_auth import make_cdp_create_headers
from app.settlement_log import log_settlement
from app.config import (
    CDP_API_KEY_ID,
    CDP_API_KEY_SECRET,
    FACILITATOR_URL,
    NETWORK,
    PUBLIC_BASE_URL,
    WALLET_ADDRESS,
)
from app.routers import (
    gleif,
    polymarket,
    sanctions,
    token_safety,
    pre_trade_verdict,
    token_dossier,
    solana_token_safety,
    solana_pretrade,
    rank_check,
    visibility_audit,
)

# --- Serveur MCP distant monté sur /mcp (remote MCP, mode découverte) ---
# Expose les 10 endpoints comme outils MCP natifs. Sans clé acheteur côté serveur,
# le serveur MCP renvoie les conditions de paiement x402. Tout échec d'init MCP est
# isolé : il NE doit PAS casser l'API de paiement.
_mcp_session_manager = None
try:
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    from mcp_server.server import build_server as _build_mcp_server

    _mcp_session_manager = StreamableHTTPSessionManager(
        app=_build_mcp_server(), json_response=True, stateless=True
    )
except Exception as _mcp_exc:  # pragma: no cover — MCP optionnel, jamais bloquant
    import sys as _sys

    print(f"[x402] MCP mount disabled: {type(_mcp_exc).__name__}: {_mcp_exc}", file=_sys.stderr)


@contextlib.asynccontextmanager
async def _lifespan(_app: "FastAPI"):
    """Démarre le gestionnaire de sessions MCP (si dispo) pour la durée de vie de l'app."""
    if _mcp_session_manager is not None:
        async with _mcp_session_manager.run():
            yield
    else:
        yield


app = FastAPI(
    title="x402-solana",
    lifespan=_lifespan,
    description=(
        "Paid x402 API tools for AI agents, settled in USDC on Solana. Crypto pre-trade safety "
        "(Solana SPL rug/honeypot checks, EVM+Solana token safety, one-call GO/NO-GO pre-trade "
        "verdicts, full token dossiers), market data (Polymarket prediction-market odds), official "
        "verification (GLEIF LEI KYB, EU sanctions/AML screening), and x402 agent discoverability "
        "(Bazaar keyword-rank pulse and signed visibility audits). Real-time, structured, "
        "machine-readable verdicts. No API key — payment is authentication. Gasless for the buyer "
        "(the facilitator is fee payer)."
    ),
    version="0.1.0",
    contact={"name": "x402-solana", "email": "joachim33333@outlook.fr"},
)

# --- Découvrabilité GLEIF (modèle réutilisé, cf CLAUDE.md Base) ---
GLEIF_DESCRIPTION = (
    "Look up any Legal Entity Identifier (LEI) for company information lookup and "
    "counterparty / know-your-business (KYB) identity against the official GLEIF global "
    "registry — returns legal name, status, jurisdiction, legal form and registered "
    "address, real-time, worldwide coverage."
)
GLEIF_OUTPUT_EXAMPLE = {
    "lei": "529900T8BM49AURSDO55",
    "legal_name": "Ubisecure Oy",
    "entity_status": "ACTIVE",
    "registration_status": "ISSUED",
    "jurisdiction": "FI",
    "legal_form_code": "DKUW",
    "legal_address": {
        "lines": ["Tekniikantie 14"],
        "city": "ESPOO",
        "region": "FI-18",
        "country": "FI",
        "postal_code": "02150",
    },
    "initial_registration_date": "2016-08-04T11:00:36Z",
    "last_update_date": "2024-06-20T07:11:02Z",
    "next_renewal_date": "2027-06-28T18:34:06Z",
    "managing_lou": "529900T8BM49AURSDO55",
    "source": "GLEIF API v1 (gleif.org)",
    "timestamp": "2026-06-24T12:00:00Z",
    "disclaimer": "Indicative data from the GLEIF register, not a compliance opinion.",
    "cached": False,
}
GLEIF_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "lei": {
            "type": "string",
            "description": "20-character LEI, e.g. '529900T8BM49AURSDO55'",
            "pattern": "^[A-Z0-9]{20}$",
        }
    },
    "required": ["lei"],
}

# --- Montage x402 seller (SVM / Solana) ---
_auth_provider = None
if CDP_API_KEY_ID and CDP_API_KEY_SECRET:
    _auth_provider = CreateHeadersAuthProvider(
        make_cdp_create_headers(FACILITATOR_URL, CDP_API_KEY_ID, CDP_API_KEY_SECRET)
    )
_facilitator = HTTPFacilitatorClient(
    FacilitatorConfig(url=FACILITATOR_URL, auth_provider=_auth_provider)
)
_server = x402ResourceServer(_facilitator)
register_exact_svm_server(_server)  # scheme exact SVM, wildcard solana:* (couvre mainnet)
_server.register_extension(bazaar_resource_server_extension)

# Borne DURE du facilitator CDP : description > 500 chars => /verify rejette le settle.
DESCRIPTION_MAX_CHARS = 500

# Registre des métadonnées brutes par path (source de vérité unique pour la
# génération des fichiers de découverte : .well-known/x402.json, agent-card.json, llms.txt).
ROUTE_META: dict[str, dict] = {}


def _route(path, price, service_name, tags, description, input_example, input_schema, output_example):
    """Fabrique une RouteConfig x402 (Solana) + discovery Bazaar."""
    if len(description) > DESCRIPTION_MAX_CHARS:
        raise ValueError(
            f"x402 route {path}: description = {len(description)} chars > {DESCRIPTION_MAX_CHARS} "
            f"(le facilitator CDP rejette le settle). Raccourcir la description de cette route."
        )
    ROUTE_META[path] = {
        "price": price,
        "service_name": service_name,
        "tags": list(tags),
        "description": description,
        "input_example": input_example,
        "input_schema": input_schema,
        "output_example": output_example,
    }
    return RouteConfig(
        accepts=PaymentOption(scheme="exact", pay_to=WALLET_ADDRESS, price=price, network=NETWORK),
        resource=f"{PUBLIC_BASE_URL}{path}",
        description=description,
        mime_type="application/json",
        service_name=service_name,
        tags=tags,
        extensions=declare_discovery_extension(
            input=input_example,
            input_schema=input_schema,
            output=OutputConfig(example=output_example),
        ),
    )


_routes = {
    # --- Vérif officielle (KYB) ---
    "GET /gleif/lei": _route(
        "/gleif/lei", "$0.01", "GLEIF LEI Lookup", ["lei", "gleif", "kyb", "company-data", "compliance"],
        GLEIF_DESCRIPTION,
        {"lei": "529900T8BM49AURSDO55"}, GLEIF_INPUT_SCHEMA, GLEIF_OUTPUT_EXAMPLE,
    ),
    # --- Vérif officielle (AML / sanctions) ---
    "GET /sanctions/screen": _route(
        "/sanctions/screen", "$0.05", "EU Sanctions Screening", ["sanctions", "aml", "watchlist", "compliance", "eu"],
        "Screen a name against the official EU consolidated sanctions list (FISMA) for anti-money-laundering "
        "(AML) and watchlist checks — returns matches with a similarity score and context (EU reference, type, programme), not a binary yes/no.",
        {"name": "Saddam Hussein", "type": "person", "threshold": 0.7, "limit": 5},
        {"type": "object", "properties": {
            "name": {"type": "string", "description": "Name to screen (person or entity), e.g. 'Saddam Hussein'"},
            "type": {"type": "string", "description": "Optional: 'person' or 'enterprise'"},
            "threshold": {"type": "number", "description": "Min similarity 0-1 to report a match (default 0.7)"},
            "limit": {"type": "integer", "description": "Max matches [1-50], e.g. 10"},
        }, "required": ["name"]},
        {"query": "Saddam Hussein", "type": "person", "threshold": 0.7, "match_count": 1,
         "matches": [{"name": "Saddam Hussein Al-Tikriti", "score": 0.78, "subject_type": "person",
                      "eu_reference": "EU.27.28", "un_id": None, "programme": "IRQ",
                      "designation_details": "Former President of Iraq", "publication_date": "2003-07-07"}],
         "list_size": 38542, "source": "EU Consolidated Financial Sanctions List — FISMA (webgate.ec.europa.eu)",
         "timestamp": "2026-06-24T15:15:00Z",
         "disclaimer": "Indicative screening against the EU consolidated list; a match is not a legal confirmation and requires human review. Not a compliance opinion.",
         "cached": False},
    ),
    # --- Market data (prediction markets) ---
    "GET /polymarket/odds": _route(
        "/polymarket/odds", "$0.05", "Polymarket Odds", ["polymarket", "prediction-market", "betting-odds", "odds", "probability"],
        "Live prediction market odds, implied probabilities and betting-market data from Polymarket — give a market id "
        "or slug, get each outcome with its probability (0-1), volume, liquidity and resolution status.",
        {"market": "2654605"},
        {"type": "object", "properties": {
            "market": {"type": "string", "description": "Polymarket market id or slug, e.g. '2654605' or 'will-it-rain-tomorrow'"},
        }, "required": ["market"]},
        {"id": "2654605", "slug": "wta-andreeva-vs-day-set-2", "question": "Set 2 Winner: Andreeva vs Day",
         "outcomes": [{"name": "Andreeva", "price": 0.06, "probability": 0.06},
                      {"name": "Day", "price": 0.94, "probability": 0.94}],
         "active": True, "closed": False, "volume": "99.98", "liquidity": "1426.8",
         "end_date": "2026-07-01T11:30:00Z", "resolution_source": None,
         "source": "Polymarket Gamma API (gamma-api.polymarket.com)", "timestamp": "2026-06-24T14:20:00Z",
         "disclaimer": "Indicative market odds, not investment advice.", "cached": False},
    ),
    # --- Safety crypto (EVM) ---
    "GET /crypto/token-safety": _route(
        "/crypto/token-safety", "$0.05", "Token Safety Check", ["token-safety", "honeypot", "rug-check", "token-security", "risk-score"],
        "Token safety check before buying — is this token a honeypot or a rug pull? Bundles honeypot detection, "
        "buy/sell tax, holder concentration, LP lock and liquidity (GoPlus, Honeypot.is, DexScreener) into a single "
        "0-100 token security risk score with a clear buy/avoid verdict. One call replaces three lookups. EVM chains.",
        {"token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "chain": "base"},
        {"type": "object", "properties": {
            "token": {"type": "string", "description": "Token contract address (0x + 40 hex), e.g. '0x833589...2913'"},
            "chain": {"type": "string", "description": "base | ethereum | bsc | polygon | arbitrum | optimism | avalanche (default base)"},
        }, "required": ["token"]},
        {"query": {"token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "chain": "base"},
         "safety_score": 95, "rating": "safe", "verdict": "Low risk on automated checks. Always DYOR.",
         "honeypot": False, "buy_tax_pct": 0.0, "sell_tax_pct": 0.0, "flags": [],
         "market": {"price_usd": 1.0, "liquidity_usd": 5200000.0, "volume_24h_usd": 18000000.0, "dex": "aerodrome", "symbol": "USDC"},
         "holder_count": "250000", "sources_ok": {"goplus": True, "honeypot_is": True, "dexscreener": True},
         "source": "GoPlus Security + Honeypot.is + DexScreener", "timestamp": "2026-06-26T16:00:00Z",
         "disclaimer": "Automated heuristic safety check, not financial advice. Always do your own research.", "cached": False},
    ),
    # --- Safety (fused GO/NO-GO) ---
    "GET /crypto/pre-trade-verdict": _route(
        "/crypto/pre-trade-verdict", "$0.05", "Pre-Trade Verdict",
        ["pre-trade-verdict", "token-safety", "go-no-go", "counterparty-screen", "trading-decision"],
        "One-call GO/CAUTION/NO-GO pre-trade verdict for AI trading agents: fuses token safety (honeypot, "
        "rug, tax, holders), counterparty wallet sanctions screening (OFAC/mixer) and cross-exchange market "
        "signal into a single decision with a signed, offline-verifiable receipt. Should I buy this token now? "
        "Replaces three separate calls (token-safety + wallet-screen + signal) with one fused GO/NO-GO verdict. "
        "EVM chains and Solana.",
        {"token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "chain": "base"},
        {"type": "object", "properties": {
            "token": {"type": "string", "description": "Token contract (EVM 0x+40hex) or SPL mint (base58) to evaluate"},
            "chain": {"type": "string", "description": "base | ethereum | bsc | polygon | arbitrum | optimism | avalanche | solana (default base)"},
            "wallet": {"type": "string", "description": "Optional counterparty wallet to screen (OFAC/mixer)"},
        }, "required": ["token"]},
        {"verdict": "GO", "confidence": 0.88,
         "reasons": [{"code": "SAFETY_OK", "label": "Token safety score 95/100 (low risk on automated checks).", "weight": -0.5},
                     {"code": "SIGNAL_NEUTRAL", "label": "Cross-exchange signal is NEUTRAL.", "weight": 0.0}],
         "query": {"chain": "base", "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "wallet": None},
         "components": {
             "token_safety": {"available": True, "kind": "evm", "safety_score": 95, "honeypot": False, "rating": "safe",
                              "flags": [], "symbol": "USDC", "buy_tax_pct": 0.0, "sell_tax_pct": 0.0},
             "counterparty_screen": {"available": False, "reason": "no wallet supplied"},
             "market_signal": {"available": True, "symbol": "USDC", "signal": "NEUTRAL", "confidence": 0.5, "fused_score": 0.0}},
         "signed_receipt": {"available": True, "algorithm": "ed25519", "public_key": "5b77...9f9d",
                            "claims": {"kind": "pre_trade_verdict", "chain": "base", "token": "0x8335...2913", "verdict": "GO",
                                       "safety_score": 95}},
         "data_freshness": {"as_of": "2026-07-02T12:00:00Z", "age_seconds": 0, "retrieved_at": "2026-07-02T12:00:00Z",
                            "deterministic": False, "sources": ["Internal token-safety", "Internal wallet-screen", "Internal signal-fusion"],
                            "components_available": ["token_safety", "market_signal"]},
         "error": None, "timestamp": "2026-07-02T12:00:00Z",
         "disclaimer": "Fused pre-trade verdict from automated safety/screening/signal checks. Heuristic, not financial advice. Always DYOR.",
         "cached": False},
    ),
    # --- Market data (deep token dossier) ---
    "GET /crypto/token-dossier": _route(
        "/crypto/token-dossier", "$0.10", "Token Dossier",
        ["token-dossier", "holders", "liquidity", "rug-check", "red-flags"],
        "Full degen token dossier in one call: safety score + honeypot/tax + detailed TOP HOLDERS and "
        "concentration + liquidity/FDV/volume/pool-age + contract control (owner, creator, mintable, "
        "open-source) + an AI red-flag narrative. Deep due-diligence report on a token before aping — the "
        "premium tier above a plain safety check. EVM chains and Solana.",
        {"token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "chain": "base"},
        {"type": "object", "properties": {
            "token": {"type": "string", "description": "Token contract (EVM 0x+40hex) or SPL mint (base58)"},
            "chain": {"type": "string", "description": "base | ethereum | bsc | polygon | arbitrum | optimism | avalanche | solana (default base)"},
        }, "required": ["token"]},
        {"query": {"token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "chain": "base"},
         "safety": {"score": 95, "rating": "safe", "honeypot": False, "buy_tax_pct": 0.0, "sell_tax_pct": 0.0, "flags": []},
         "holders": {"count": "250000", "top": [{"address": "0xabc...", "percent": 0.08, "is_contract": True, "tag": "Aerodrome"}], "top10_share_pct": 42.1},
         "liquidity": {"liquidity_usd": 5200000.0, "fdv_usd": 40000000000.0, "volume_24h_usd": 18000000.0,
                       "price_usd": 1.0, "price_change_24h_pct": 0.01, "pool_age_hours": 9000.0, "dex": "aerodrome", "symbol": "USDC"},
         "contract": {"open_source": True, "mintable": True, "owner_address": None, "creator_address": None, "is_proxy": True, "can_take_back_ownership": False},
         "narrative": {"summary": "USDC is a fully-collateralized blue-chip stablecoin with deep liquidity; automated checks show no rug/honeypot signals.",
                       "red_flags": [], "bottom_line": "Low automated risk; standard stablecoin.", "mode": "llm"},
         "data_freshness": {"as_of": "2026-07-02T12:00:00Z", "age_seconds": 0, "retrieved_at": "2026-07-02T12:00:00Z",
                            "deterministic": False, "sources": ["GoPlus Security", "Honeypot.is", "DexScreener", "Claude Haiku (narrative)"],
                            "narrative_mode": "llm", "chain_kind": "evm"},
         "error": None, "source": "GoPlus Security + Honeypot.is + DexScreener + Claude Haiku (narrative)",
         "timestamp": "2026-07-02T12:00:00Z",
         "disclaimer": "Automated token dossier (safety + holders + liquidity + narrative). Heuristic, not financial advice. Always DYOR.",
         "cached": False},
    ),
    # --- Safety Solana (SPL rug/honeypot) ---
    "GET /solana/token-safety": _route(
        "/solana/token-safety", "$0.01", "Solana Token Safety Pro",
        ["solana-token-safety", "rug-check", "honeypot", "behavioral-analysis", "spl-token-risk"],
        "Solana SPL token safety, rug check and honeypot / scam detection before trading: is this Solana token a rug pull, "
        "honeypot or scam? Verdict SAFE/RISKY/CRITICAL + 0-100 score combining STATIC checks (mint/freeze authority, holder "
        "concentration) AND BEHAVIORAL analysis (liquidity, churn, recent dump, tx velocity) plus a blue-chip false-positive "
        "guard so USDC/USDT/SOL are never flagged. Pre-trade SPL token security / scam-token detection catching behavioral "
        "rugs static checkers miss.",
        {"mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "deep": False},
        {"type": "object", "properties": {
            "mint": {"type": "string", "description": "SPL token mint address (base58), e.g. 'EPjFW...Dt1v' (USDC)"},
            "deep": {"type": "boolean", "description": "Deeper behavioral + holder analysis (more RPC calls)"},
        }, "required": ["mint"]},
        {"verdict": "RISKY", "confidence": 0.8, "score": 62,
         "reasons": [{"code": "COMPOSITE_SCORE", "label": "Composite safety score 62/100", "weight": 0.2}],
         "query": {"mint": "So11111111111111111111111111111111111111112", "deep": False},
         "static_flags": [{"code": "MINT_AUTHORITY_ACTIVE", "label": "Mint authority not renounced — supply can be inflated", "weight": 0.6}],
         "behavioral_flags": [{"code": "LIQUIDITY_TO_FDV_THIN", "label": "Liquidity is 1.4% of FDV — top-heavy valuation", "weight": 0.5}],
         "behavioral_status": "ok",
         "false_positive_guard": {"triggered": False, "note": "Not whitelisted; behavioral context applied to avoid static-only over-flagging."},
         "concentration": {"top1_share": 0.41, "top5_share": 0.66, "note": "Largest account may be an AMM/LP pool, not a malicious whale."},
         "market": {"liquidity_usd": 240000.0, "fdv_usd": 17000000.0, "volume_24h_usd": 980000.0,
                    "price_change_24h_pct": -8.2, "pool_age_hours": 220.5, "symbol": "WIF", "dex": "raydium"},
         "mint_info": {"mint_authority": None, "freeze_authority": None, "decimals": 6, "is_token_2022": False},
         "sources_ok": {"solana_rpc": True, "dexscreener": True},
         "data_freshness": {"as_of": "2026-06-29T12:00:00Z", "age_seconds": 0, "retrieved_at": "2026-06-29T12:00:00Z",
                            "deterministic": False, "sources": ["Solana RPC (mint, largest accounts, signatures)", "DexScreener (liquidity, pool age, price)"],
                            "behavioral_status": "ok"},
         "error": None, "timestamp": "2026-06-29T12:00:00Z",
         "disclaimer": "Automated heuristic safety check (static + behavioral), not financial advice. Always DYOR.", "cached": False},
    ),
    # --- Safety Solana (all-in-one pre-trade) ---
    "GET /solana/pre-trade": _route(
        "/solana/pre-trade", "$0.05", "Solana Pre-Trade Bundle",
        ["solana-pre-trade", "trading-decision", "liquidity-depth", "deployer-history", "all-in-one"],
        "All-in-one Solana pre-trade decision in ONE call: BUY-SAFE/CAUTION/AVOID fusing four scored modules — token "
        "security, EXECUTABLE liquidity depth (estimated slippage at $100/$1k/$10k), deployer history/control and holder "
        "concentration. Should I buy or avoid this Solana token? Full token due-diligence in one call — replaces 3-4 lookups; "
        "built for a trading / sniping agent's risk-review pipeline. Solana trading safety decision and buy/avoid verdict.",
        {"mint": "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"},
        {"type": "object", "properties": {
            "mint": {"type": "string", "description": "SPL token mint address (base58) to evaluate before buying"},
        }, "required": ["mint"]},
        {"verdict": "CAUTION", "confidence": 0.75, "composite_score": 64,
         "reasons": [{"code": "MODERATE_DEPTH", "label": "~3.1% impact on a $1k trade", "weight": 0.2},
                     {"code": "AUTHORITIES_RENOUNCED", "label": "Mint & freeze authority renounced — deployer retains no control", "weight": -0.4}],
         "query": {"mint": "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"},
         "module_weights": {"token_security": 0.4, "liquidity_depth": 0.25, "deployer_history": 0.2, "holder_concentration": 0.15},
         "modules": {
             "token_security": {"sub_score": 70, "status": "ok", "static_flags": [], "behavioral_flags": [], "behavioral_status": "ok"},
             "liquidity_depth": {"status": "ok", "sub_score": 70, "liquidity_usd": 320000.0,
                                 "slippage_estimates": [{"trade_usd": 100, "est_price_impact_pct": 0.06},
                                                        {"trade_usd": 1000, "est_price_impact_pct": 0.62},
                                                        {"trade_usd": 10000, "est_price_impact_pct": 5.88}],
                                 "reasons": [{"code": "MODERATE_DEPTH", "label": "~0.6% impact on a $1k trade", "weight": 0.2}],
                                 "note": "Estimated from pooled liquidity via a constant-product model; real slippage depends on routing."},
             "deployer_history": {"status": "renounced", "sub_score": 80, "controller": None,
                                  "reasons": [{"code": "AUTHORITIES_RENOUNCED", "label": "Mint & freeze authority renounced", "weight": -0.4}],
                                  "note": "Deployer no longer controls the mint."},
             "holder_concentration": {"status": "ok", "sub_score": 60, "top1_share": 0.34, "top5_share": 0.58,
                                      "reasons": [{"code": "MODERATE_CONCENTRATION", "label": "Top account 34%", "weight": 0.2}],
                                      "note": "Largest account may be an AMM/LP pool, not a malicious whale."}},
         "market": {"liquidity_usd": 320000.0, "fdv_usd": 9000000.0, "volume_24h_usd": 1200000.0,
                    "price_change_24h_pct": 4.1, "pool_age_hours": 5200.0, "symbol": "WIF", "dex": "raydium"},
         "data_freshness": {"as_of": "2026-06-29T12:00:00Z", "age_seconds": 0, "retrieved_at": "2026-06-29T12:00:00Z",
                            "deterministic": False, "sources": ["Solana RPC", "DexScreener"], "kill_switch": False, "behavioral_status": "ok"},
         "error": None, "timestamp": "2026-06-29T12:00:00Z",
         "disclaimer": "All-in-one pre-trade decision (security + executable depth + deployer + concentration). Heuristic, not advice. Always DYOR.",
         "cached": False},
    ),
    # --- Ranking (cheap discoverability pulse) ---
    "GET /agent/rank-check": _route(
        "/agent/rank-check", "$0.10", "Agent Rank Check",
        ["rank-check", "x402-discoverability", "bazaar-ranking", "keyword-rank", "discovery-monitor"],
        "Quick check of where an x402 seller ranks RIGHT NOW by keyword-relevance in the CDP Bazaar discovery "
        "(not the raw settled-volume rank the free explorers show): best rank + per-category-keyword rank in one "
        "cheap call, plus a pointer to the full /agent/visibility-audit when the rank slips. The frequent pulse for "
        "monitoring your x402 discoverability. Where do I rank now? Am I being out-ranked on my category keywords?",
        {"seller": "api.example.com"},
        {"type": "object", "properties": {
            "seller": {"type": "string", "description": "Seller to check: wallet (0x + 40 hex or Solana base58) or origin URL/domain, e.g. 'api.example.com'"},
        }, "required": ["seller"]},
        {"seller": "api.example.com", "best_rank": 14, "headline": "Best rank #14 (outside the top 10) — you're being out-ranked.",
         "per_keyword": [{"keyword": "kyb", "rank": 14, "scanned": 20}, {"keyword": "compliance", "rank": None, "scanned": 20}],
         "category_keywords": ["kyb", "compliance", "vat"],
         "recommendation": "Rank is slipping on ['compliance'] → run /agent/visibility-audit for the metadata score, prioritized fixes and a signed delta over time.",
         "upsell": "/agent/visibility-audit",
         "note": "Keyword-RELEVANCE rank in CDP Bazaar discovery/search — not the raw settled-volume rank the free explorers show.",
         "error": None, "timestamp": "2026-07-02T12:00:00Z", "cached": False},
    ),
    # --- Ranking (premium signed audit) ---
    "GET /agent/visibility-audit": _route(
        "/agent/visibility-audit", "$1.00", "Agent Visibility Audit",
        ["visibility-audit", "discoverability", "bazaar-ranking", "aeo-geo", "discovery-seo"],
        "Audit how discoverable an x402 agent/seller is across the agent registries (CDP Bazaar, 402index): "
        "keyword-RELEVANCE rank per category (not raw settled-volume rank), a metadata-quality score of your "
        "advertised endpoints (schema, output.example, tags, llm_usage_prompt), settle activity, a top-3 "
        "benchmark, prioritized fixes, and a DELTA vs a signed snapshot you carry back. Why am I not being found "
        "and how do I climb? GEO/AEO discovery audit for x402 sellers, Ed25519-signed.",
        {"seller": "api.example.com"},
        {"type": "object", "properties": {
            "seller": {"type": "string", "description": "Seller to audit: wallet (0x + 40 hex or Solana base58) or origin URL/domain, e.g. 'api.example.com'"},
            "snapshot": {"type": "string", "description": "Optional: the signed_snapshot JSON from a previous audit, to compute a dated delta"},
        }, "required": ["seller"]},
        {"overall_score": 62, "scores": {"metadata": 78, "keyword_rank": 45, "settle_activity": 55},
         "summary": "Solid metadata but mid-pack keyword rank (#14 for 'kyb', absent for 'compliance'). Top fix: add a real output.example to endpoints missing one.",
         "identity": {"declared_name": "Example x402 API", "discovery_available": True, "endpoints_scored": 6},
         "keyword_rank": {"category_keywords": ["kyb", "compliance", "vat"], "best_rank": 14, "note": "Keyword-relevance rank in CDP Bazaar discovery (not settled-volume rank).", "bazaar_available": True},
         "benchmark": {"available": True, "category": "kyb", "top3": ["https://a.com/x", "https://b.com/y", "https://c.com/z"]},
         "settle_activity": {"available": True, "verdict": "CAUTION", "trust_score": 62, "settlement_count": 11},
         "prioritized_fixes": [{"issue": "output_example", "endpoints_affected": 3, "impact": 90, "fix": "Add a real output.example to each endpoint."}],
         "delta": {"available": True, "since": "2026-06-28T12:00:00Z", "overall_delta": 9, "per_keyword": [{"keyword": "kyb", "previous_rank": 28, "current_rank": 14, "places_gained": 14}]},
         "signed_snapshot": {"available": True, "algorithm": "ed25519",
                             "claims": {"kind": "agent_visibility_snapshot", "seller": "api.example.com", "overall_score": 62, "best_keyword_ranks": {"kyb": 14}, "as_of": "2026-07-02T12:00:00Z"},
                             "signature": "<hex>"},
         "snapshot_usage": "Pass signed_snapshot back as ?snapshot= next time for a dated delta.",
         "timestamp": "2026-07-02T12:00:00Z",
         "disclaimer": "Discoverability audit; scores/deltas deterministic and signed, summary is explanation only. Not a guarantee of placement.",
         "cached": False},
    ),
}

_x402_gate = payment_middleware(_routes, _server)

# Table route -> prix (pour enrichir le log de règlement avec le montant attendu).
_ROUTE_PRICE: dict[str, str] = {}
for _k, _rc in _routes.items():
    _opt = _rc.accepts[0] if isinstance(_rc.accepts, (list, tuple)) else _rc.accepts
    _ROUTE_PRICE[_k] = str(getattr(_opt, "price", "")).lstrip("$")


@app.middleware("http")
async def x402_payment_gate(request, call_next):
    """Passerelle x402 : exige le paiement Solana sur les routes déclarées dans _routes."""
    response = await _x402_gate(request, call_next)
    prh = response.headers.get(PAYMENT_RESPONSE_HEADER)
    if prh:
        try:
            sr = decode_payment_response_header(prh)
            if getattr(sr, "success", False):
                route_key = f"{request.method} {request.url.path}"
                log_settlement(
                    route=request.url.path,
                    method=request.method,
                    tx=getattr(sr, "transaction", None),
                    payer=getattr(sr, "payer", None),
                    amount=_ROUTE_PRICE.get(route_key),
                    network=getattr(sr, "network", None) or NETWORK,
                    success=True,
                )
        except Exception:  # noqa: BLE001 — le log ne doit jamais casser la réponse
            pass
    return response


app.include_router(gleif.router)
app.include_router(sanctions.router)
app.include_router(polymarket.router)
app.include_router(token_safety.router)
app.include_router(pre_trade_verdict.router)
app.include_router(token_dossier.router)
app.include_router(solana_token_safety.router)
app.include_router(solana_pretrade.router)
app.include_router(rank_check.router)
app.include_router(visibility_audit.router)


@app.get("/health")
async def health() -> JSONResponse:
    """Santé globale du service (route gratuite, non protégée par x402)."""
    return JSONResponse(
        content={
            "status": "ok",
            "service": "x402-solana",
            "network": NETWORK,
            "facilitator": FACILITATOR_URL,
            "pay_to": WALLET_ADDRESS,
            "endpoints": [
                "/gleif/lei", "/sanctions/screen", "/polymarket/odds",
                "/crypto/token-safety", "/crypto/pre-trade-verdict", "/crypto/token-dossier",
                "/solana/token-safety", "/solana/pre-trade",
                "/agent/rank-check", "/agent/visibility-audit",
            ],
        }
    )


# --- Fichiers de découverte servis par l'app (routes gratuites, non gated x402) ---
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _serve_file(relative_path: str, media_type: str) -> FileResponse:
    file_path = _PROJECT_ROOT / relative_path
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail=f"Fichier de découverte introuvable: {relative_path}")
    return FileResponse(file_path, media_type=media_type)


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> FileResponse:
    return _serve_file("favicon.ico", "image/x-icon")


@app.get("/.well-known/x402.json", include_in_schema=False)
async def wellknown_x402() -> FileResponse:
    return _serve_file(".well-known/x402.json", "application/json")


@app.get("/.well-known/x402", include_in_schema=False)
async def wellknown_x402_noext() -> FileResponse:
    return _serve_file(".well-known/x402.json", "application/json")


@app.get("/.well-known/agent-card.json", include_in_schema=False)
async def wellknown_agent_card() -> FileResponse:
    return _serve_file(".well-known/agent-card.json", "application/json")


@app.get("/llms.txt", include_in_schema=False)
async def llms_txt() -> FileResponse:
    return _serve_file("llms.txt", "text/plain; charset=utf-8")


# Preuve de propriété du domaine pour 402index. Sert UNIQUEMENT le hash SHA-256
# public (jamais le verification_token secret, gardé dans .env non commité).
# text/plain, sans redirect, < 1KB. Override possible via l'env INDEX402_VERIFICATION_HASH.
_INDEX402_VERIFICATION_HASH = os.environ.get(
    "INDEX402_VERIFICATION_HASH",
    "4f81dba181722ba1e5533fbba87e9ca8772043ec56e0ddf81edd542c96cbb501",
)


@app.get("/.well-known/402index-verify.txt", include_in_schema=False)
async def wellknown_402index_verify() -> PlainTextResponse:
    return PlainTextResponse(_INDEX402_VERIFICATION_HASH, media_type="text/plain; charset=utf-8")


# --- Endpoint MCP distant (route ASGI brute, hors paywall x402) ---
# Les agents MCP HTTP (ex. connectors Claude.ai) s'y connectent pour découvrir et
# appeler les 10 endpoints comme outils. Non listé dans _routes -> non gated.
if _mcp_session_manager is not None:

    async def _handle_mcp(scope, receive, send):
        await _mcp_session_manager.handle_request(scope, receive, send)

    app.router.routes.append(Mount("/mcp", app=_handle_mcp))
