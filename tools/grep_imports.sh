#!/usr/bin/env bash
cd /home/joachim/x402-endpoints/app/routers || exit 1
for f in solana_pretrade solana_token_safety token_safety pre_trade_verdict token_dossier polymarket gleif sanctions rank_check visibility_audit; do
  echo "=== $f ==="
  grep -E "^from app\.|^import app|from app\.config import|from app\.sources|from app\.routers" "$f.py" | sort -u
done
