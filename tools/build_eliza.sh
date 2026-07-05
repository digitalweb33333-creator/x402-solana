#!/usr/bin/env bash
set -euo pipefail
export PATH="$HOME/.nvm/versions/node/v24.18.0/bin:$PATH"
cd /home/joachim/x402-solana/eliza-plugin
echo "node: $(node -v) | npm: $(npm -v) | which npm: $(which npm)"
rm -rf node_modules package-lock.json
npm install --no-audit --no-fund 2>&1 | tail -4
echo "=== build ==="
npm run build 2>&1 | tail -20
echo "=== dist ==="
ls -la dist/ 2>&1 | head
