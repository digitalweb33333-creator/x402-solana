# x402-endpoints — Serveur MCP

Expose les **28 endpoints payants** du catalogue x402-endpoints comme **outils MCP
natifs**, utilisables par Claude Desktop, Cursor, Claude.ai (connectors), LangChain,
ou tout client MCP. Paiement par appel via le protocole **x402** (USDC sur **Base mainnet**).

La liste d'outils est générée depuis `../.well-known/x402.json` → elle ne diverge
jamais du catalogue réellement déployé. Chaque outil porte sa description sémantique
et son **prix** (ex. `$0.01` ou `$0.05`).

## Deux modes

| Mode | Quand | Comportement d'un appel d'outil |
|---|---|---|
| **Auto-pay** | une clé d'un wallet acheteur **financé en USDC (Base)** est configurée | paie le micro-paiement x402 et renvoie la **vraie donnée** |
| **Découverte** (défaut) | pas de clé | renvoie les **conditions de paiement** (prix, réseau, asset, `pay_to`, resource) + un exemple de sortie |

> ⚠️ Ne **jamais** mettre une clé acheteur sur un serveur public. L'auto-pay est
> destiné à un usage **local** (la machine de l'agent, avec son propre wallet).

## Variables d'environnement

| Variable | Défaut | Rôle |
|---|---|---|
| `X402_BASE_URL` | `https://x402-endpoints.onrender.com` | base de l'API appelée |
| `X402_BUYER_PRIVATE_KEY` | — | clé privée d'un wallet **Base financé en USDC** (active l'auto-pay). `BUYER_PRIVATE_KEY` est aussi accepté |
| `X402_AUTO_PAY` | `1` si une clé est présente | mettre `0` pour forcer le mode découverte |
| `X402_NETWORK` | `eip155:8453` | réseau de paiement (Base mainnet) |

Diagnostic : `python -m mcp_server --info`

## Accès concret

### 1. Claude Desktop / Cursor (stdio, auto-pay) — recommandé

Ajouter au fichier de config MCP (`claude_desktop_config.json`, ou réglages MCP de Cursor) :

```json
{
  "mcpServers": {
    "x402-endpoints": {
      "command": "/home/joachim/x402-endpoints/.venv/bin/python",
      "args": ["-m", "mcp_server"],
      "cwd": "/home/joachim/x402-endpoints",
      "env": {
        "X402_BUYER_PRIVATE_KEY": "0xVOTRE_CLE_WALLET_BASE_FINANCE_USDC",
        "X402_AUTO_PAY": "1"
      }
    }
  }
}
```

L'agent voit alors 28 outils (`gleif_lei`, `crypto_token_safety`, `crypto_derivatives_radar`,
`sanctions_screen`, …) et les appelle comme des fonctions natives. Chaque appel paie
automatiquement le prix indiqué et renvoie la donnée live.

Sans le bloc `env` (ou avec `X402_AUTO_PAY=0`), même config en **mode découverte**.

### 2. Remote MCP (Claude.ai connector / client MCP HTTP)

Le serveur est monté sur l'API déployée :

```
https://x402-endpoints.onrender.com/mcp
```

À ajouter comme **connector MCP distant** (Streamable HTTP). En remote, le serveur
tourne en **mode découverte** (pas de wallet côté serveur) : il liste les 28 outils et
renvoie les conditions de paiement. Idéal pour la **découverte** du catalogue par les agents.

### 3. Self-host HTTP

```bash
python -m mcp_server --transport http --host 0.0.0.0 --port 8001
# -> endpoint MCP : http://<host>:8001/mcp
```

### 4. LangChain (langchain-mcp-adapters)

```python
from langchain_mcp_adapters.client import MultiServerMCPClient

client = MultiServerMCPClient({
    "x402_endpoints": {
        "transport": "streamable_http",
        "url": "https://x402-endpoints.onrender.com/mcp",
    }
    # ou en stdio + auto-pay :
    # "x402_endpoints": {
    #     "transport": "stdio",
    #     "command": "/home/joachim/x402-endpoints/.venv/bin/python",
    #     "args": ["-m", "mcp_server"],
    #     "env": {"X402_BUYER_PRIVATE_KEY": "0x...", "X402_AUTO_PAY": "1"},
    # }
})
tools = await client.get_tools()   # 28 outils LangChain prêts à brancher sur un agent
```

## Catalogue (extrait)

`gleif_lei`, `vies_vat`, `uk_companies_search`, `fr_sirene_lookup`, `sanctions_screen`,
`eurlex_search`, `fr_legifrance_search`, `cve_lookup`, `recalls_search`, `drug_label`,
`patents_search`, `ted_tenders`, `ecb_exchange_rate`, `iban_validate`, `polymarket_odds`,
`crypto_token_safety`, `crypto_derivatives_radar`, `crypto_wallet_xray`,
`crypto_dex_cex_spread`, `crypto_new_pairs`, `web_extract`, … (28 au total).

Liste complète et à jour : `python -m mcp_server --info` puis l'API `/.well-known/x402.json`.
