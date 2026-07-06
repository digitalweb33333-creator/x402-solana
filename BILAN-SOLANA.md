# BILAN-SOLANA — Distribution & découvrabilité x402-solana

**Date :** 2026-07-06 · **Service :** https://x402-solana-cva8.onrender.com (10 endpoints payants, USDC sur Solana mainnet, facilitator CDP, gasless buyer)
**Seller :** `CucGfdmABDC3QvaZdn9AwUfYBCmmvYjTDdq3WBHXDLEF` · **npm :** `plugin-x402-solana@0.1.0`

Audit direct des canaux réalisé en live (`tools/audit_channels.py`, `tools/bazaar_rank.py`) + vérifications web/API.

---

## 1. Tableau de présence par canal

| Canal | Statut réel | Rank / détail | Action restante |
|---|---|---|---|
| **npm** | ✅ **Listé** | `plugin-x402-solana@0.1.0` publié | — |
| **CDP Bazaar** | ✅ **Listé (10/10)** | **rank #1 sur 6/8 requêtes clés**, #2 sur 2 | fraîcheur (settle) — voir §3 |
| **MCP Registry (officiel)** | ✅ **Listé, actif** | `io.github.digitalweb33333-creator/x402-solana` v0.1.0 `status=active` | — |
| **Glama (glama.ai/mcp)** | ✅ **Listé** | connector auto-synchronisé depuis le MCP Registry | — |
| **mcp.so** | ⏳ **Propagation** | agrège le MCP Registry (fetch direct bloqué 403 bot) | revérifier l'UI sous qq jours |
| **x402scan** | ✅ **Enregistré** (cette session) | API `register-origin` → HTTP 200, **10 endpoints indexés** (source `openapi`) ; page `/origin/…` en propagation | revérifier l'UI |
| **402index.io** | ⏳ **Enregistré 10/10** (cette session) | `201 pending review` sur les 10 | **MANUEL** : *verify domain* pour approbation instantanée |
| **awesome-agentic-commerce** (Merit-Systems) | ⏳ **PR ouverte** | [#408](https://github.com/Merit-Systems/awesome-agentic-commerce/pull/408) — `MERGEABLE` | attente merge mainteneur |
| **awesome-x402** (xpaysh) | ⏳ **PR ouverte** (cette session) | [#739](https://github.com/xpaysh/awesome-x402/pull/739) — section *Data & Social APIs* | attente merge mainteneur |
| **ElizaOS registry** (elizaOS/eliza) | ⏳ **PR ouverte** (cette session) | [#15056](https://github.com/elizaOS/eliza/pull/15056) — entrée third-party + wire-format régénéré | attente merge mainteneur |
| **Smithery** | ❌ **Pas inscrit** | exige connexion GitHub + build côté Smithery (pas d'API d'auto-submit) | **MANUEL** — voir §2 |
| **agentic.market** | ❔ **Non applicable** | aucun registre/API public d'auto-inscription trouvé | surveiller |
| **Solana-natif** (solana.com/x402) | ✅ **Couvert** | pas de registre Solana séparé : la découverte Solana passe par le **Bazaar (réseau `solana:…`)** + x402scan + 402index, tous couverts | — |

**Nouveau créé cette session :** repo standalone [`digitalweb33333-creator/plugin-x402-solana`](https://github.com/digitalweb33333-creator/plugin-x402-solana) (artefact ElizaOS installable, requis par la PR registry).

---

## 2. Inscriptions — automatisées vs manuelles

### Faites automatiquement cette session
- **402index.io** — 10/10 endpoints (ré)enregistrés via `tools/register_402index.py` (API, idempotent).
- **x402scan** — origine enregistrée via SIWX (`tools/register_x402scan.py`), 10 endpoints indexés via OpenAPI, HTTP 200.
- **awesome-x402 (xpaysh)** — PR #739 ouverte (fork + insert + PR).
- **ElizaOS registry** — repo standalone créé et poussé, PR #15056 ouverte (entrée source `entries/third-party/plugin-x402-solana.json` + `generated-registry.json` régénéré à l'identique du transform officiel ; vérifié : les 21 entrées upstream intactes, +1 ajoutée).

### Déjà en place (vérifié)
- **npm**, **MCP Registry officiel**, **Glama**, **awesome-agentic-commerce PR #408**.

### ⚠️ Actions MANUELLES requises de ta part
1. **402index — vérification de domaine** : les 10 services sont `pending review`. Pour l'approbation instantanée, valider la propriété du domaine (méthode meta-tag/DNS proposée dans le dashboard 402index, compte lié à `joachim33333@outlook.fr`). Sans ça, revue manuelle par 402index (le probe live des endpoints devrait passer).
2. **Smithery** : pas d'auto-submit. Se connecter sur smithery.ai avec le GitHub `digitalweb33333-creator`, soumettre le serveur MCP (repo `x402-solana`, endpoint `/mcp/`). Manuel.
3. **Merges de PR** (hors de notre contrôle) : #408, #739, #15056 attendent la revue des mainteneurs.

---

## 3. Rank Bazaar — leviers appliqués et honnêteté sur les limites

### Position actuelle (mesurée live, `tools/bazaar_rank.py`)
| Requête | Total | Solana | Nous | Best rank |
|---|---|---|---|---|
| solana pre-trade | 10 | 3 | 3 | **#1** |
| solana token safety | 18 | 6 | 4 | **#1** |
| GLEIF LEI | 17 | 1 | 1 | **#1** |
| polymarket odds | 16 | 1 | 1 | **#1** |
| token dossier | 11 | 1 | 1 | **#1** |
| x402 visibility audit | 4 | 1 | 1 | **#1** |
| sanctions screening | 19 | 1 | 1 | #2 |
| solana rug honeypot | 16 | 2 | 2 | #2 |

**#1 sur 6/8 requêtes clés.**

### ✅ Sous notre contrôle — déjà maximisé
- **Qualité des métadonnées** : chaque endpoint du discovery (`/.well-known/x402.json`) porte une description en langage-question dense en mots-clés (« Should I buy this token now? », « rug pull, honeypot or scam? »), des `tags`, un `input` schema complet et un `output.example` réaliste. C'est précisément ce qui nous met #1 sur 6/8. Rien de significatif à gratter sans réécrire du texte déjà saturé en mots-clés (gain marginal, risque de redeploy).
- **Discovery bien exposé** : `/.well-known/x402.json` + alias `/.well-known/x402` + `/.well-known/agent-card.json` + `/llms.txt`, tous live. Maximisé.

### 🟡 Sous notre contrôle mais coûte (levier fraîcheur)
- **Fraîcheur des settlements** : 20 règlements réels on-chain le 2026-07-05 (cf. `SETTLEMENTS.md`). Le score `settle_activity`/`trust` du Bazaar décroît avec le temps → un nouveau settle le ré-augmente. **Actionnable mais dépense de l'USDC réel** ; solde buyer actuel ≈ **0.116 USDC** (couvre un refresh ciblé des 2 endpoints faibles `/sanctions/screen` $0.05 + `/solana/token-safety` $0.01 = **$0.06**, pas un refresh complet à 1.47 USDC). **En attente de ton feu vert** (mouvement de fonds irréversible).

### ❌ Hors de notre contrôle (honnêteté)
- Le **rank absolu #1 sur `sanctions screening` et `solana rug honeypot`** est détenu par un concurrent établi. Le classement Bazaar mêle relevance sémantique (qu'on maximise déjà) ET volume settlé / trust on-chain (piloté par le facilitator CDP + le volume tiers du concurrent). Aucune édition de métadonnées ne renverse de façon fiable une position #1 adossée au volume. Un settle ciblé de $0.06 n'y suffira probablement pas seul.

---

## 4. Résumé exécutif
- **Couverture** : présents/soumis sur **tous les canaux pertinents** (11 canaux + repo plugin). 3 PR ouvertes cette session (xpaysh, ElizaOS, +#408 pré-existante), 2 annuaires (ré)enregistrés par API (402index, x402scan).
- **Rank** : **#1 sur 6/8** requêtes Bazaar — métadonnées et discovery déjà au plafond.
- **À faire côté Joachim** : (1) verify domain 402index, (2) submit Smithery, (3) feu vert pour un settle de fraîcheur ciblé ($0.06). Les merges de PR dépendent des mainteneurs.
