#!/usr/bin/env bash
set -euo pipefail
cd /home/joachim/x402-solana
PY=/home/joachim/.local/share/uv/python/cpython-3.12.13-linux-x86_64-gnu/bin/python3.12
"$PY" -m venv .venv
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet "x402[evm,svm,extensions]==2.13.1"
./.venv/bin/pip install --quiet -r requirements.txt
echo "=== imports check ==="
./.venv/bin/python -c "import x402, solders, solana, fastapi, uvicorn, nacl, httpx; print('x402', x402.__version__, '| imports OK')"
echo "=== resolved solders/solana ==="
./.venv/bin/pip show solders solana 2>/dev/null | grep -E "^Name|^Version"
