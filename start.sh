#!/usr/bin/env bash
# Start Outpost in Docker on Linux or macOS. Safe to run again: it rebuilds if
# needed, keeps all data, and only creates an admin the first time.
set -euo pipefail
cd "$(dirname "$0")"

command -v docker >/dev/null || { echo "Install Docker first: https://docs.docker.com/get-docker/"; exit 1; }
docker info >/dev/null 2>&1 || { echo "Docker isn't running. Start it, then run this again."; exit 1; }

# The address contestants type: this machine's LAN/hotspot IP.
ip=""
if [[ "$(uname)" == "Darwin" ]]; then
  for dev in en0 en1 bridge100; do ip=$(ipconfig getifaddr "$dev" 2>/dev/null || true); [[ -n "$ip" ]] && break; done
else
  ip=$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^(192\.168|10\.|172\.(1[6-9]|2[0-9]|3[01]))\.' | grep -v '^172\.17\.' | head -n1 || true)
fi
export OUTPOST_PUBLIC_URL="${OUTPOST_PUBLIC_URL:-${ip:+http://$ip:8000/}}"

docker compose up -d --build
docker compose exec -T outpost outpost admin jury --if-missing
echo "Checking compilers and the sandbox..."
docker compose exec -T outpost outpost selftest || echo "!! The self-test found problems (above). Fix them before a contest."

echo
echo "Outpost is running."
echo "  Admin:       http://localhost:8000/admin   (user: jury; the password is printed above the first time)"
echo "  Contestants: ${OUTPOST_PUBLIC_URL:-set the address on the admin dashboard}"
echo "  Stop:        docker compose down        (data is kept)"
