"""Configuration centrale du projet x402-solana (rail de paiement Solana).

Réplique la config Base mais route les paiements sur Solana mainnet (USDC SPL)
via le MÊME facilitator CDP (qui supporte nativement Solana, cf
DIFFERENCES-BASE-SOLANA.md). Les clés (CDP, upstream, Anthropic) ne sont JAMAIS
écrites en clair ici : elles sont lues depuis l'environnement à l'exécution
(sourcé du .env Base en local, dashboard Render en prod). Le .env de ce projet
ne contient que les 3 valeurs publiques Solana (seller, mint, RPC).
"""

import os

from dotenv import load_dotenv

load_dotenv()

# --- Paiement x402 seller (Solana) ---
# Adresse publique du seller Solana (base58) = payTo des routes.
SOLANA_SELLER_ADDRESS: str = os.getenv("SOLANA_SELLER_ADDRESS", "").strip()
# Alias générique consommé par main.py (comme WALLET_ADDRESS côté Base).
WALLET_ADDRESS: str = SOLANA_SELLER_ADDRESS

# Mint USDC Solana mainnet (6 décimales) — résolu auto par le SDK, exposé pour info.
SOLANA_USDC_MINT: str = os.getenv(
    "SOLANA_USDC_MINT", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
).strip()
SOLANA_RPC_URL: str = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com").strip()

# Facilitator de production : CDP (identique à Base ; supporte Solana mainnet).
FACILITATOR_URL: str = os.getenv(
    "FACILITATOR_URL", "https://api.cdp.coinbase.com/platform/v2/x402"
).strip()

# Réseau de paiement : Solana mainnet (CAIP-2). L'USDC est résolu automatiquement
# par le SDK x402 (mint EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v, 6 décimales).
NETWORK: str = os.getenv("X402_NETWORK", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp").strip()

# --- Auth facilitator CDP (production) — mêmes clés que Base, lues de l'env ---
CDP_API_KEY_ID: str = os.getenv("CDP_API_KEY_ID", "").strip()
CDP_API_KEY_SECRET: str = os.getenv("CDP_API_KEY_SECRET", "").strip()

# Base URL publique (construit le champ `resource` des routes pour la découverte).
PUBLIC_BASE_URL: str = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").strip()

# --- Clés des sources upstream (lues de l'env ; endpoints copiés de Base) ---
OPENSKY_CLIENT_ID: str = os.getenv("OPENSKY_CLIENT_ID", "").strip()
OPENSKY_CLIENT_SECRET: str = os.getenv("OPENSKY_CLIENT_SECRET", "").strip()
OPENCHARGEMAP_API_KEY: str = os.getenv("OPENCHARGEMAP_API_KEY", "").strip()
NVD_API_KEY: str = os.getenv("NVD_API_KEY", "").strip()
OPENFDA_API_KEY: str = os.getenv("OPENFDA_API_KEY", "").strip()
GOOGLE_SOLAR_API_KEY: str = os.getenv("GOOGLE_SOLAR_API_KEY", "").strip()
COMPANIES_HOUSE_API_KEY: str = os.getenv("COMPANIES_HOUSE_API_KEY", "").strip()
EPO_OPS_KEY: str = os.getenv("EPO_OPS_KEY", "").strip()
EPO_OPS_SECRET: str = os.getenv("EPO_OPS_SECRET", "").strip()
INSEE_SIRENE_API_KEY: str = os.getenv("INSEE_SIRENE_API_KEY", "").strip()
LEGIFRANCE_CLIENT_ID: str = os.getenv("LEGIFRANCE_CLIENT_ID", "").strip()
LEGIFRANCE_CLIENT_SECRET: str = os.getenv("LEGIFRANCE_CLIENT_SECRET", "").strip()

# LLM (endpoints composés — dégradent proprement sans clé).
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL: str = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5").strip()

# RPC Base (endpoints EVM copiés mais NON montés côté Solana ; gardés pour import).
ALCHEMY_BASE_KEY: str = os.getenv("ALCHEMY_BASE_KEY", "").strip()
ALCHEMY_BASE_URL: str = os.getenv("ALCHEMY_BASE_URL", "").strip()

if not SOLANA_SELLER_ADDRESS:
    raise RuntimeError(
        "SOLANA_SELLER_ADDRESS manquant dans .env — requis comme payTo des routes x402 Solana."
    )
