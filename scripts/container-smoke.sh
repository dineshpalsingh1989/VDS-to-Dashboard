#!/usr/bin/env bash
# Exercise the real Linux container with no broker/PLC access or published ports.
set -Eeuo pipefail
image="${1:-wilcoxon-vds:ci}"
name="vds-check-${RANDOM}-$$"
cleanup() { docker rm -f "$name" >/dev/null 2>&1 || true; }
trap cleanup EXIT
docker run -d --name "$name" --network none --read-only \
  --tmpfs /data:uid=10001,gid=10001,mode=0700 --tmpfs /tmp \
  --cap-drop ALL --security-opt no-new-privileges:true "$image" >/dev/null
for attempt in {1..60}; do
  if docker exec "$name" python -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8050/healthz", timeout=2)' >/dev/null 2>&1; then
    break
  fi
  if [[ "$attempt" == 60 ]]; then docker logs "$name"; exit 1; fi
  sleep 1
done
docker exec -i "$name" python - <<'PY'
import glob
import json
import os
import sqlite3
import time
import urllib.request

assert os.getuid() == 10001, 'Container must run as the service user'
for route in ('/', '/_dash-layout', '/_dash-dependencies'):
    with urllib.request.urlopen('http://127.0.0.1:8050' + route, timeout=10) as response:
        assert response.status == 200, route
for attempt in range(30):
    backups = glob.glob('/data/backups/vds-*.db')
    if backups:
        break
    time.sleep(1)
assert backups, 'Startup maintenance did not create a backup'
connection = sqlite3.connect(backups[0])
try:
    assert connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
finally:
    connection.close()
print('PASS: non-root Linux container, read-only filesystem, HTTP routes, and verified backup')
PY
