#!/usr/bin/env bash
set -euo pipefail
cd /home/joachim/x402-solana
git add -A
git commit -q -F - <<'MSG'
Add settlement record + distribution tooling

10/10 on-chain settlements (tx hashes in SETTLEMENTS.md). Adds 402index
registration + CDP Bazaar probe scripts (post-deploy ready).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
MSG
git push -q origin main
echo "pushed"
git log --oneline -3
