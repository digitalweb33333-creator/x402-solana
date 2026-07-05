#!/usr/bin/env bash
set -euo pipefail
cd /home/joachim/x402-solana
git add -A
if git ls-files | grep -E '(^|/)\.env$|buyer/\.env$'; then echo "ABORT secret"; exit 1; fi
git commit -q -F - <<'MSG'
Add remote MCP server (/mcp) + MCP registry OIDC publish

Mounts the 10 endpoints as native MCP tools at /mcp (discovery mode, no buyer key
server-side). server.json + mcp-publish.yml publish to the official MCP registry
via GitHub OIDC. Directory tooling: x402scan SIWX + 402index full payload.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
MSG
git push -q origin main
echo "pushed"; git log --oneline -1
