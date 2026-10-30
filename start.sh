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
  # By interface, not by address range: Docker's own bridges (docker0, br-*)
  # use private addresses too, and one must never end up on the login slips.
  candidates=$( { ip -4 -o addr show scope global 2>/dev/null \
                  | awk '$2 !~ /^(docker|br-|veth|virbr|lxc|cni|flannel|tailscale|zt)/ {split($4, a, "/"); print a[1]}'; } \
                || hostname -I 2>/dev/null | tr ' ' '\n')
  # A hotspot or home network (192.168.x) first, then anything else left.
  ip=$(printf '%s\n' "$candidates" | grep -m1 '^192\.168\.' || printf '%s\n' "$candidates" | grep -m1 . || true)
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
