#!/usr/bin/env bash
set -euo pipefail
SRC=/home/joachim/x402-endpoints
DST=/home/joachim/x402-solana
rsync -a --exclude "__pycache__" "$SRC/app/" "$DST/app/"
cp "$SRC/favicon.ico" "$DST/favicon.ico"
cp "$SRC/requirements.txt" "$DST/requirements.txt"
echo "=== copied ==="
echo "routers: $(ls "$DST/app/routers"/*.py | wc -l)"
echo "sources: $(ls "$DST/app/sources"/*.py | wc -l)"
find "$DST/app" -maxdepth 1 -type f -name "*.py" | sort
