#!/usr/bin/env bash
set -euo pipefail
cd /home/joachim/x402-solana
git add -A
git commit -q -F - <<'MSG'
Fix public URL to the live Render host (x402-solana-cva8)

Render assigned the -cva8 suffix; PUBLIC_BASE_URL and the discovery files now
point at https://x402-solana-cva8.onrender.com so the 402 resource.url and
agent-card resolve to the live host (required for Bazaar/x402scan crawling).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
MSG
git push -q origin main
echo "pushed"; git log --oneline -1
