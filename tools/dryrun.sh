#!/usr/bin/env bash
set -uo pipefail
cd /home/joachim/x402-solana
source tools/env_local.sh
# démarre uvicorn en arrière-plan
./.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --log-level warning >/tmp/x402sol_uvicorn.log 2>&1 &
UVPID=$!
trap 'kill $UVPID 2>/dev/null' EXIT
# attend la disponibilité
for i in $(seq 1 40); do
  if curl -s -o /dev/null http://127.0.0.1:8000/health; then break; fi
  sleep 0.5
done
echo "=== uvicorn up (pid $UVPID) ==="
./.venv/bin/python tools/dryrun_check.py
RC=$?
echo "=== uvicorn log (tail) ==="
tail -8 /tmp/x402sol_uvicorn.log
exit $RC
