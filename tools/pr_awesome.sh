#!/usr/bin/env bash
set -euo pipefail
cd /tmp/awesome-agentic-commerce
git config user.email "joachim33333@outlook.fr"
git config user.name "digitalweb33333-creator"
git checkout -b add-x402-solana

ENTRY='- [x402-solana](https://x402-solana-cva8.onrender.com) - Paid x402 API tools for AI agents settled in USDC on **Solana** (CDP facilitator, gasless for the buyer). 10 endpoints: crypto/Solana pre-trade safety (SPL rug/honeypot, GO/NO-GO verdicts, token dossiers), market data (Polymarket odds), official KYB/AML verification (GLEIF LEI, EU sanctions), and x402 discoverability audits. Remote [MCP server](https://x402-solana-cva8.onrender.com/mcp/), listed on 402index. No API key — payment is authentication.'

# Insère l'entrée à la fin de la section "### Ecosystem" (juste avant la section suivante).
python3 - "$ENTRY" <<'PY'
import sys, io
entry = sys.argv[1]
p = "README.md"
lines = open(p, encoding="utf-8").read().split("\n")
out, in_eco = [], False
inserted = False
for i, ln in enumerate(lines):
    if ln.strip() == "### Ecosystem":
        in_eco = True
        out.append(ln); continue
    if in_eco and ln.startswith("### ") and ln.strip() != "### Ecosystem":
        # fin de section Ecosystem -> insère avant
        if out and out[-1].strip() != "":
            out.append(entry)
        else:
            out.insert(len(out), entry)
        out.append("")
        in_eco = False; inserted = True
    out.append(ln)
if not inserted and in_eco:
    out.append(entry)
open(p, "w", encoding="utf-8").write("\n".join(out))
print("inserted:", inserted)
PY

grep -n "x402-solana" README.md | head
git add README.md
git commit -q -m "Add x402-solana to Ecosystem (paid x402 API tools on Solana)"
git push -q -u origin add-x402-solana 2>&1 | tail -3
gh pr create --repo Merit-Systems/awesome-agentic-commerce \
  --title "Add x402-solana to Ecosystem" \
  --body "Adds **x402-solana** to the Ecosystem section: paid x402 API tools for AI agents settled in USDC on Solana (CDP facilitator, gasless buyer). 10 live endpoints spanning crypto/Solana pre-trade safety, market data, official KYB/AML verification, and x402 discoverability. Live at https://x402-solana-cva8.onrender.com (health 200), remote MCP server at /mcp/, published to the MCP registry and listed on 402index. No API key — payment is authentication.

🤖 Generated with [Claude Code](https://claude.com/claude-code)" 2>&1 | tail -3
