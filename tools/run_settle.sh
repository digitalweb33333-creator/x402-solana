#!/usr/bin/env bash
# Runner settlement. Démarre uvicorn, source les env (secrets Base + buyer local),
# lance tools/settle.py en passant les arguments reçus ($@).
# DRY par défaut ; --execute pour un settlement réel (APRÈS le "go").
set -uo pipefail
cd /home/joachim/x402-solana
source tools/env_local.sh
set -a; source buyer/.env; set +a
./.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --log-level warning >/tmp/x402sol_uvicorn.log 2>&1 &
UVPID=$!
trap 'kill $UVPID 2>/dev/null' EXIT
for i in $(seq 1 40); do curl -s -o /dev/null http://127.0.0.1:8000/health && break; sleep 0.5; done
echo "=== uvicorn up (pid $UVPID) ==="
./.venv/bin/python -m tools.settle "$@"
RC=$?
echo "=== uvicorn log (tail) ==="; tail -6 /tmp/x402sol_uvicorn.log
exit $RC
