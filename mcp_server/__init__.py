"""Serveur MCP du catalogue x402-endpoints.

Expose les 28 endpoints payants (x402, USDC sur Base mainnet) comme outils MCP
natifs, consommables par Claude Desktop, Cursor, LangChain, etc.

Source unique de vérité : ../.well-known/x402.json (généré depuis app/main.py),
donc la liste d'outils ne diverge jamais du catalogue réellement déployé.

Deux modes (selon l'environnement) :
- AUTO-PAY : si une clé d'un wallet acheteur financé (USDC Base) est fournie,
  chaque appel d'outil paie automatiquement via le client httpx x402 et renvoie
  la VRAIE donnée. C'est le mode « ça marche tout seul » pour un agent qui finance
  un wallet. À n'utiliser QUE localement, jamais sur un serveur public.
- DÉCOUVERTE (défaut, sans clé) : un appel d'outil renvoie les conditions de
  paiement décodées (prix, réseau, asset, pay_to, resource) + l'exemple de sortie,
  pour que l'agent découvre le catalogue et son coût. Aucun secret requis.
"""
