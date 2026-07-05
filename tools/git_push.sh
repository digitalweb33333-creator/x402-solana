#!/usr/bin/env bash
set -euo pipefail
cd /home/joachim/x402-solana
git config user.email "joachim33333@outlook.fr"
git config user.name "digitalweb33333-creator"
git add -A
# double garde-fou : refuse si un vrai secret est suivi
if git ls-files | grep -E '(^|/)\.env$|buyer/\.env$'; then
  echo "ABORT: un fichier .env secret est suivi par git"; exit 1
fi
git commit -q -m "$(cat <<'MSG'
x402-solana: 10 paid endpoints on the Solana rail (USDC, CDP facilitator)

Solana-rail replica of x402-endpoints (Base). SVM exact scheme, gasless buyer
(facilitator feePayer). 10 endpoints: crypto/Solana safety, market data,
official verification (KYB/AML), x402 discoverability. Discovery files +
Render blueprint + settlement tooling included. No secrets committed.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
MSG
)"
echo "=== committed ==="
git log --oneline -1
# crée le repo GitHub public et pousse (main)
git branch -M main
if gh repo view digitalweb33333-creator/x402-solana >/dev/null 2>&1; then
  echo "repo existe déjà — push"
  git remote add origin https://github.com/digitalweb33333-creator/x402-solana.git 2>/dev/null || true
  git push -u origin main
else
  gh repo create digitalweb33333-creator/x402-solana --public --source=. --remote=origin --push \
    --description "Paid x402 API tools for AI agents, settled in USDC on Solana. Crypto/Solana pre-trade safety, market data, KYB/AML verification, x402 discoverability. No API key — payment is authentication."
fi
echo "=== remote ==="
git remote -v
