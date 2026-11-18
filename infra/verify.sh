#!/usr/bin/env bash
# Negative tests: without an Easy Auth session nothing may reach the app (no 200 anywhere).
# Usage: infra/verify.sh https://<app-fqdn>
set -uo pipefail
URL="${1:?usage: verify.sh https://<fqdn>}"
fail=0
for i in $(seq 1 12); do
  ok=1
  for path in / /health /api/me /api/whoami "/api/genie/messages/x/y"; do
    code=$(curl -s -m 10 -o /dev/null -w "%{http_code}" "$URL$path")
    [[ "$code" == "200" ]] && ok=0
  done
  [[ $ok == 1 ]] && break
  echo "still serving 200 without auth (propagation?), retry $i/12"; sleep 10
done
for path in / /health /api/me /api/whoami; do
  code=$(curl -s -m 10 -o /dev/null -w "%{http_code} -> %{redirect_url}" "$URL$path")
  echo "unauthenticated GET $path : ${code:0:90}"
  [[ "$code" == 200* ]] && fail=1
done
code=$(curl -s -m 10 -o /dev/null -w "%{http_code}" -X POST "$URL/api/genie/ask" -H 'content-type: application/json' -d '{"question":"x"}')
echo "unauthenticated POST /api/genie/ask : $code"; [[ "$code" == "200" ]] && fail=1
code=$(curl -s -m 10 -o /dev/null -w "%{http_code}" "${URL/https:/http:}/health")
echo "plain HTTP /health : $code (expect 301/302/308)"
if [[ $fail == 1 ]]; then echo "VERIFY FAILED: app reachable without authentication"; exit 1; fi
echo "VERIFY OK: every path requires Easy Auth"
