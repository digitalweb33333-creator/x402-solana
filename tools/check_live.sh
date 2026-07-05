#!/usr/bin/env bash
set -uo pipefail
U=https://x402-solana-cva8.onrender.com
cd /home/joachim/x402-solana
echo "=== discovery files ==="
for f in .well-known/x402.json .well-known/agent-card.json llms.txt; do
  code=$(curl -sS -m 30 -o /dev/null -w "%{http_code}" "$U/$f"); echo "  /$f -> $code"
done
echo "=== resource URL in served x402.json ==="
curl -sS -m 30 "$U/.well-known/x402.json" | ./.venv/bin/python -c "import sys,json; d=json.load(sys.stdin); print('  name:',d.get('name')); print('  network:',d.get('network')); print('  n resources:',len(d.get('resources',[]))); print('  first resource:',d['resources'][0]['resource'])"
echo "=== raw 402 header resource (gleif) ==="
curl -sS -m 30 -D - -o /dev/null "$U/gleif/lei?lei=529900T8BM49AURSDO55" 2>/dev/null | grep -iE "^payment-required:|^www-authenticate:" | head -1 > /tmp/hdr.txt
./.venv/bin/python - <<'PY'
import base64, json
raw = open('/tmp/hdr.txt').read().strip()
if ':' in raw:
    val = raw.split(':',1)[1].strip()
    tok = val.split()[-1] if ' ' in val else val
    try:
        d = json.loads(base64.b64decode(tok + '='*(-len(tok)%4)))
        print('  402 resource:', d.get('resource') or (d.get('accepts',[{}])[0].get('resource')))
        a = (d.get('accepts') or [{}])[0]
        print('  402 network:', a.get('network'), '| payTo:', a.get('payTo'), '| amount:', a.get('maxAmountRequired') or a.get('amount'))
    except Exception as e:
        print('  decode error:', e, '| raw head:', raw[:80])
else:
    print('  no payment header captured')
PY
