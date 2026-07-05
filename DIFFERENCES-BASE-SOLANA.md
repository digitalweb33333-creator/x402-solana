# x402 Base → Solana — Analyse du pattern & différences (Étape 1)

**Date :** 2026-07-05
**Référence (lecture seule) :** `~/x402-endpoints` — 52 endpoints live, seller Base `0x1D1B81247C407521E2A01F3E21514870dcf1620f`.
**Cible :** `~/x402-solana` — même qualité, rail de paiement Solana (USDC SPL).

---

## 1. Pattern Base (vérifié dans le code)

- **Stack** : FastAPI. `app/main.py` monte la passerelle x402 en middleware HTTP ; un dict `_routes` mappe `"METHOD /path" → RouteConfig`.
- **SDK** : `x402` 2.13.1. Montage seller :
  - `x402ResourceServer(HTTPFacilitatorClient(FacilitatorConfig(url, auth_provider)))`
  - `register_exact_evm_server(server)` → scheme `exact` EVM, wildcard `eip155:*`.
  - `server.register_extension(bazaar_resource_server_extension)` → enrichit la découverte Bazaar.
- **Route** : `RouteConfig(accepts=PaymentOption(scheme="exact", pay_to=WALLET, price="$X", network=NETWORK), resource, description, mime_type, service_name, tags, extensions=declare_discovery_extension(input, input_schema, output))`.
- **Facilitator** : CDP production `https://api.cdp.coinbase.com/platform/v2/x402`, auth Bearer JWT Ed25519 par requête (`app/cdp_auth.py`).
- **Découvrabilité** : `.well-known/x402.json`, `agent-card.json`, `llms.txt`. Description = [Action] + [donnée] + [SOURCE D'AUTORITÉ] + [couverture].
- **Contrainte DURE** : `description ≤ 500 caractères`, sinon le `/verify` CDP rejette le settle (erreur trompeuse « paymentPayload invalid »). Guard fail-fast au démarrage (`DESCRIPTION_MAX_CHARS`).
- **Receipts** : `app/receipt.py` signe Ed25519 (`RECEIPT_SIGNING_SEED`), vérifiable hors-ligne.
- **Handlers** : `app/routers/*.py` — wrappers FastAPI **agnostiques du paiement** (ils ne renvoient que de la donnée). La couche paiement est 100 % dans `main.py`.

## 2. Support Solana — VÉRIFIÉ en live (read-only, `tools/check_facilitator.py`)

`GET /supported` sur le facilitator CDP renvoie **4 kinds Solana** :

| x402Version | network | scheme | feePayer |
|---|---|---|---|
| 2 | `solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp` (mainnet) | exact | `Hc3sdEAsCGQcpgfivywog9uwtk8gUBUZgsxdME1EJy88` |
| 2 | `solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1` (devnet) | exact | idem |
| 1 | `solana` (mainnet, legacy) | exact | idem |
| 1 | `solana-devnet` | exact | idem |

→ **Le même facilitator CDP, les mêmes clés CDP et le même `cdp_auth.py` fonctionnent pour Solana.** Aucun facilitator tiers requis.

## 3. Différences Base vs Solana (ce qui change dans le code)

| Aspect | Base (EVM) | Solana (SVM) |
|---|---|---|
| `NETWORK` | `eip155:8453` | `solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp` |
| Mechanism serveur | `register_exact_evm_server` | `register_exact_svm_server` |
| Extra pip | `x402[evm]` | `x402[svm]` (solders + solana) |
| `pay_to` | `0x…` (hex 40) | base58 (32-44) — `CucGfdmABDC3QvaZdn9AwUfYBCmmvYjTDdq3WBHXDLEF` |
| Asset USDC | `0x833589…` (ERC-20) | mint `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v` |
| Décimales USDC | 6 | 6 (identique) |
| Mécanique settle | EIP-3009 `transferWithAuthorization` | **transfert SPL** : le buyer construit une VersionedTransaction (transfer USDC ATA→ATA + compute-budget + memo), signe comme *token authority* ; le **facilitator est feePayer** (co-signe + submit). Gasless côté buyer. |
| Prérequis on-chain | approve/allowance | **ATA USDC** du buyer ET du seller doivent exister (sinon instruction create-ATA idempotente, rent payé par feePayer) |
| Signer buyer | eth-account (clé hex) | `KeypairSigner.from_base58(<clé base58>)` |
| Facilitator + auth | CDP, Bearer JWT Ed25519 | **identique** |
| Description ≤ 500 | oui | oui (contrainte CDP, tous réseaux) |
| Découverte / receipts / health | — | **identiques** (agnostiques du réseau) |

## 4. Conséquence architecture

- Les **routers** (`app/routers/*.py`) et **sources** sont copiés **verbatim** (agnostiques du paiement).
- Seuls changent : `config.py` (network/seller/mint Solana), `main.py` (SVM au lieu d'EVM, 10 routes), `receipt.py` (issuer = domaine Solana), les fichiers de découverte.
- **Secrets** : les clés CDP + clés upstream ne sont **jamais écrites en clair** dans `~/x402-solana`. Injectées à l'exécution via l'environnement (sourcé du `.env` Base en local, dashboard Render en prod). Le `.env` Solana ne contient que les 3 valeurs publiques Solana. `buyer/.env` (clé privée) est gitignoré (`*.env`).

## 5. Sélection des 10 endpoints prioritaires (budget-aware)

Buyer de test : ~1.58 USDC. Un settle par endpoint doit tenir dans ce budget → somme des 10 = **$1.47**.

| # | Endpoint | Prix | Catégorie |
|---|---|---|---|
| 1 | /solana/pre-trade | $0.05 | Safety Solana (all-in-one BUY-SAFE/CAUTION/AVOID) |
| 2 | /solana/token-safety | $0.01 | Safety Solana (rug/honeypot SPL) |
| 3 | /crypto/token-safety | $0.05 | Safety crypto (honeypot/tax/LP lock) |
| 4 | /crypto/pre-trade-verdict | $0.05 | Safety (GO/CAUTION/NO-GO) |
| 5 | /crypto/token-dossier | $0.10 | Market data (top holders/liquidité) |
| 6 | /polymarket/odds | $0.05 | Market data (prediction markets) |
| 7 | /gleif/lei | $0.01 | Vérif officielle (KYB LEI) |
| 8 | /sanctions/screen | $0.05 | Vérif officielle (AML/sanctions EU) |
| 9 | /agent/rank-check | $0.10 | Ranking (pulse découvrabilité Bazaar) |
| 10 | /agent/visibility-audit | $1.00 | Ranking (audit premium Ed25519-signé) |

Endpoints premium écartés du lot initial car ils dépasseraient le budget d'un settle-each et/ou dépendent d'infra Base (EVM RPC) : `/agent/clearance-packet` ($1.50), `/agent/passport` ($1.00, ERC-8004 sur Base). Ajoutables plus tard si le buyer est réapprovisionné.
