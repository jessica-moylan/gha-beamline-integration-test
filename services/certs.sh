#!/usr/bin/env bash
# Generate the throwaway self-signed Redis TLS cert the compose stack mounts.
# Usage: services/certs.sh <redis-host-name>   (SAN = that name + 127.0.0.1)
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
host="${1:-redis}"
mkdir -p "$here/certs"
openssl req -x509 -newkey rsa:2048 -sha256 -days 1 -nodes \
  -keyout "$here/certs/redis.key" -out "$here/certs/redis.crt" \
  -subj "/CN=redis (beamline CI, self-signed throwaway)" \
  -addext "subjectAltName=DNS:${host},IP:127.0.0.1" >/dev/null 2>&1
# 0644 on purpose: the redis container drops to uid 999 and must read the key;
# it is a one-day throwaway cert bound to loopback and never committed.
chmod 644 "$here/certs/redis.key" "$here/certs/redis.crt"
echo "$here/certs"
