"""Insère l'entrée x402-solana dans un README awesome, au bon endroit.
Usage: python insert_entry.py <readme_path> <mode:xpaysh|solana>"""
import sys

README = sys.argv[1]
MODE = sys.argv[2]

XPAYSH = (
    "- [x402-solana](https://x402-solana-cva8.onrender.com) - 10 pay-per-call API "
    "tools for AI agents, settled in USDC on **Solana** mainnet via the Coinbase CDP "
    "facilitator (gasless for the buyer). Crypto & Solana pre-trade safety (SPL/EVM "
    "rug & honeypot checks, GO/NO-GO verdicts, token dossiers), market data (Polymarket "
    "odds), official KYB/AML (GLEIF LEI, EU sanctions screening), and x402 discoverability "
    "audits. $0.01–$1.00 USDC per call, no API keys. v2 with Bazaar discovery extension. "
    "([Discovery](https://x402-solana-cva8.onrender.com/.well-known/x402.json)) "
    "([MCP](https://x402-solana-cva8.onrender.com/mcp/)) "
    "([npm](https://www.npmjs.com/package/plugin-x402-solana)) "
    "([MCP Registry](https://registry.modelcontextprotocol.io/v0/servers?search=x402-solana)) "
    "([GitHub](https://github.com/digitalweb33333-creator/x402-solana))"
)

SOLANA = (
    "- [x402-solana](https://x402-solana-cva8.onrender.com) - 10 pay-per-call API tools "
    "for AI agents on Solana — SPL/EVM token-safety and pre-trade GO/NO-GO verdicts, token "
    "dossiers, GLEIF LEI and EU sanctions/AML screening, and Polymarket odds — settled in "
    "USDC via the Coinbase CDP facilitator with gas sponsored (no SOL needed), exposed as a "
    "remote MCP server and an ElizaOS plugin, no API keys."
)

# (anchor line that STARTS the section AFTER ours -> insert just before it)
NEXT_SECTION = {
    "xpaysh": "### Games & On-Chain Apps",
    "solana": "## Developer Tools",
}[MODE]
ENTRY = {"xpaysh": XPAYSH, "solana": SOLANA}[MODE]

lines = open(README, encoding="utf-8").read().split("\n")
out = []
inserted = False
for ln in lines:
    if not inserted and ln.strip() == NEXT_SECTION:
        # remonter au dernier bullet non vide déjà écrit, insérer après
        j = len(out) - 1
        while j >= 0 and out[j].strip() == "":
            j -= 1
        out.insert(j + 1, ENTRY)
        inserted = True
    out.append(ln)

if not inserted:
    raise SystemExit(f"ANCHOR NOT FOUND: {NEXT_SECTION!r}")

open(README, "w", encoding="utf-8").write("\n".join(out))
print(f"inserted x402-solana before {NEXT_SECTION!r} in {README}")
