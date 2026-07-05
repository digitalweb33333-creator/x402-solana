# x402-solana — Settlements réels (preuve on-chain)

**Date :** 2026-07-05 · **Réseau :** Solana mainnet (`solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp`)
**Asset :** USDC `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v` (6 décimales)
**Buyer :** `CZynG3Gsst8DqgLJL3uFmQi1ZE3y9eraKBYNrp9GdSE5` → **Seller :** `CucGfdmABDC3QvaZdn9AwUfYBCmmvYjTDdq3WBHXDLEF`
**Facilitator (feePayer, gasless buyer) :** CDP `Hc3sdEAsCGQcpgfivywog9uwtk8gUBUZgsxdME1EJy88`

## Prérequis — création de l'ATA USDC du seller
- ATA seller : `49dKVdrvNESCJ74Ypaoe3PCX1EkFgqD4Hn24FrFJqQay`
- tx : `2khBaULYsCdj38qTjAULQrWSTcAHRwyVnsQg2BwSv1AkvnVtwUHnbfWYY3iZWBABR52EcL2EbJFuX73D997eBr3B` (rent ~0.00204 SOL, payé par le buyer)

## Settlements — 1 paiement réel par endpoint (10/10 réussis)

| # | Endpoint | USDC | Tx signature |
|---|---|---|---|
| 1 | /gleif/lei | 0.01 | `5Y4pWZodADCmwUQ5eVN9MaPLmkkRep4dQWBEKrsri7QxjoHwgUMbYBmbobykrdhwVcWZcfJFT51d7zRyUR4qhSNU` |
| 2 | /solana/token-safety | 0.01 | `5VQJos7KUT4APHecamvf6NZiqnv3PxZzMdCTwCFgHn95f1fc7TbWnWEFycUSNVjFkiHpSwfioPALwKK6PrB5doQ1` |
| 3 | /sanctions/screen | 0.05 | `3MF21zxN5DQFHNitnoYQCAaW316ZWAy14rs7fDYcrasZ9JBKbAoU7vgB8Gg9XCz89VTJmQG5AUErCJkck1HHnHKg` |
| 4 | /polymarket/odds | 0.05 | `3GKfHtMdNXTw36MRvhc8GjjAczPx6wdcPAy1NSZhgho6oPEf1Efuh9xAKdFaV2A8jSH4YEtRHMkovvntMKjHbP4y` |
| 5 | /crypto/token-safety | 0.05 | `2B4rci3moAiSVwBZi22UKWJ2rjDZ4cXSapSZNDb1mbqPXzhg86nRtyAEN48UWx6qwnQdGSWBV2QdsHBx1Yht1gCe` |
| 6 | /crypto/pre-trade-verdict | 0.05 | `4bpugXviLXea5es4R5HmHHP2spGE54NJqhfVn5xCfHNF6YVDBKJfjeiBqyEKEoakM4zA4uAAFdpX4sDQmdh8H34i` |
| 7 | /solana/pre-trade | 0.05 | `3i6EgKJcxmXQy3XBjXv2MUqNnqD3BdQmN76B77bcGwL3AL8NZNyBMgx89y7XkfmNQ2UgSY5sr5mJPFQfb5zaTVFs` |
| 8 | /crypto/token-dossier | 0.10 | `5HCVd28B6w6pUgfoGKvpGwR7sVzHNPJR5ohbFgGmet77XtAMeQ5VSb1yWLHkSVFnF8aAhr5VLTR3xMfS6wzqEWK` |
| 9 | /agent/rank-check | 0.10 | `9FgCM3GPCtir3YYoHeiQoyaBRdSJV9rXK2cvfgcoznUMpfVKdMDeJcigRHEkaYCi1aNdJFZQtFzwMtiNMbYRBKb` |
| 10 | /agent/visibility-audit | 1.00 | `fSnFYgyyyfJFhvvFjtrQXgs5u4ru9hsfyxDb41UJVovkePm521uWoEYmyWVhZdB6YBz7PNcqVdQjuZ6TktqCFaz` |

**Total : 1.47 USDC · 10/10 settlements.** Frais buyer : 0 (gasless).

## Vérification des soldes (post-settlements, on-chain)
- Buyer : **0.11602 USDC** (1.58602 − 1.47) · 0.015830 SOL
- Seller : **1.47 USDC** (reçus) · ATA `49dKVdrv…`

## Note root-cause (transparence)
Les 2 endpoints agent-meta (`rank-check`, `visibility-audit`) ont d'abord renvoyé **502 NO_CATEGORY**
(non chargés) car testés avec un seller placeholder `api.example.com` sans document de découverte.
Corrigé en pointant un vrai seller x402 découvrable (`x402-endpoints.onrender.com`) → 200 + settle OK.
Aucun bug de paiement ; les 8 autres avaient settlé du premier coup.
