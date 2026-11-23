#!/usr/bin/env bash
# Start the whole stack the way a newcomer does (example config, no .env) and check that
# every service comes up and stays up. Used by CI; needs Linux (host networking) and free
# ports 8765 and 8090. Run from the repo: bash .github/compose-smoke.sh
set -euo pipefail

cp config.example.toml config.toml
export AGENTC_UID AGENTC_GID
AGENTC_UID=$(id -u)
AGENTC_GID=$(id -g)
cleanup() {
  status=$?
  [ "$status" -ne 0 ] && docker compose logs --no-color  # the logs, only when it failed
  docker compose down
  exit "$status"
}
trap cleanup EXIT

docker compose up -d --build

has_reading() {  # the logger saved a first row (a failed read: the example plugs do not exist)
  python3 - <<'EOF'
import sqlite3, sys
try:
    n = sqlite3.connect("file:data/energy.db?mode=ro", uri=True).execute(
        "SELECT COUNT(*) FROM readings").fetchone()[0]
except sqlite3.Error:
    n = 0
sys.exit(0 if n else 1)
EOF
}

ready=""
for _ in $(seq 1 30); do  # up to 150 s: the logger reads on the next whole minute
  sleep 5
  if curl -fsS localhost:8765/health >/dev/null 2>&1 \
     && curl -fsS localhost:8765/ >/dev/null 2>&1 \
     && curl -fsS localhost:8090/v1/health >/dev/null 2>&1 \
     && [ -s data/secret ] && has_reading; then
    ready=1
    break
  fi
done

echo "--- services: name, state, restarts"
docker inspect -f '{{.Name}} {{.State.Status}} {{.RestartCount}}' $(docker compose ps -aq)
bad=$(docker inspect -f '{{.State.Status}} {{.RestartCount}}' $(docker compose ps -aq) \
      | awk '$1 != "running" || $2 != 0' | wc -l)

if [ -z "$ready" ] || [ "$bad" -ne 0 ]; then
  echo "FAIL: ready=${ready:-no}, services down or restarting: $bad"
  exit 1
fi
echo "OK: logger, jobs, inbox and ntfy are up; a first reading and the secret are saved"
