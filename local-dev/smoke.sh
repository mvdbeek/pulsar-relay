#!/usr/bin/env bash
#
# Verify the local-dev stack: list providers, exercise device flow with a
# scripted Keycloak sign-in, then prove the resulting access token works.
#
# Usage:  ./smoke.sh
# Requires: docker compose up -d (already), curl, jq, python3.

set -euo pipefail

RELAY="${RELAY:-http://localhost:9000}"
KC="${KC:-http://localhost:8080}"
KC_USER="${KC_USER:-alice}"
KC_PASS="${KC_PASS:-alicepass}"

step() { printf "\n\033[1;34m==>\033[0m %s\n" "$*"; }

step "Health-check the relay"
curl -sf "$RELAY/health" >/dev/null && echo "  relay is up"

step "List configured OIDC providers"
curl -sf "$RELAY/auth/oidc/providers" | jq

step "Request a device code"
DEVICE_JSON=$(curl -sf -X POST "$RELAY/auth/device/code" -d "client_hint=smoke.sh on $(hostname)")
DEVICE_CODE=$(jq -r .device_code <<<"$DEVICE_JSON")
USER_CODE=$(jq -r .user_code <<<"$DEVICE_JSON")
VERIFY_URL=$(jq -r .verification_uri_complete <<<"$DEVICE_JSON")
INTERVAL=$(jq -r .interval <<<"$DEVICE_JSON")
echo "  user_code:    $USER_CODE"
echo "  verify URL:   $VERIFY_URL"
echo "  poll every:   ${INTERVAL}s"

step "Approve the device session in the background by driving Keycloak's login form"

# Prefer the project's venv (already has httpx); fall back to any python3 with httpx installed.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_PY="$SCRIPT_DIR/../.venv/bin/python"
if [ -x "$PROJECT_PY" ] && "$PROJECT_PY" -c "import httpx" 2>/dev/null; then
  PY="$PROJECT_PY"
elif command -v python3 >/dev/null && python3 -c "import httpx" 2>/dev/null; then
  PY=python3
else
  echo "ERROR: need python3 with the httpx package."
  echo "  - run smoke.sh from the repo root after \`pip install -e .[dev]\`,"
  echo "  - or:  pip install httpx  (and re-run)."
  exit 1
fi

"$PY" - "$RELAY" "$USER_CODE" "$KC_USER" "$KC_PASS" <<'PY' &
import sys, re, httpx
from html import unescape
from http.cookies import SimpleCookie

relay, user_code, username, password = sys.argv[1:]

# Walk the redirect chain manually so Secure/SameSite=None cookies survive HTTP.
def walk_get(client, url, cookies):
    while True:
        h = {"Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items())} if cookies else {}
        r = client.get(url, headers=h)
        for raw in r.headers.get_list("set-cookie"):
            for k, v in SimpleCookie(raw).items():
                cookies[k] = v.value
        if r.status_code in (301, 302, 303, 307, 308):
            loc = r.headers["location"]
            url = loc if loc.startswith("http") else url.rsplit("/", 1)[0] + loc
            continue
        return r

with httpx.Client(timeout=15.0, follow_redirects=False) as client:
    cookies = {}
    start = client.get(f"{relay}/auth/oidc/keycloak/login", params={"device_user_code": user_code})
    page = walk_get(client, start.headers["location"], cookies)
    action = unescape(re.search(r'kc-form-login["\'][^>]*action=["\']([^"\']+)', page.text).group(1))
    cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
    submit = client.post(action, data={"username": username, "password": password, "credentialId": ""},
                         headers={"Cookie": cookie_header})
    for raw in submit.headers.get_list("set-cookie"):
        for k, v in SimpleCookie(raw).items():
            cookies[k] = v.value
    cb = client.get(submit.headers["location"], headers={"Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items())})
    cb.raise_for_status()
    print("  operator: device session approved")
PY
APPROVE_PID=$!

step "Daemon polls /auth/device/token"
ACCESS_TOKEN=""; REFRESH_TOKEN=""
deadline=$(( $(date +%s) + 120 ))
while [ "$(date +%s)" -lt "$deadline" ]; do
  sleep "$INTERVAL"
  RESP=$(curl -s -X POST "$RELAY/auth/device/token" \
    -d "grant_type=urn:ietf:params:oauth:grant-type:device_code" \
    -d "device_code=$DEVICE_CODE")
  STATUS=$(jq -r '.error // "ok"' <<<"$RESP")
  case "$STATUS" in
    ok)
      ACCESS_TOKEN=$(jq -r .access_token <<<"$RESP")
      REFRESH_TOKEN=$(jq -r .refresh_token <<<"$RESP")
      echo "  tokens received"
      break ;;
    authorization_pending) echo "  ... pending" ;;
    slow_down) INTERVAL=$((INTERVAL+5)); echo "  ... slow_down (interval now ${INTERVAL}s)" ;;
    *) echo "  ERROR: $RESP"; exit 1 ;;
  esac
done

wait "$APPROVE_PID" || true
if [ -z "$ACCESS_TOKEN" ]; then echo "device flow timed out"; exit 1; fi

step "Identify the user via GET /auth/me"
curl -sf "$RELAY/auth/me" -H "Authorization: Bearer $ACCESS_TOKEN" | jq

step "Rotate the refresh token"
ROTATED=$(curl -sf -X POST "$RELAY/auth/token/refresh" \
  -H "Content-Type: application/json" \
  -d "{\"refresh_token\":\"$REFRESH_TOKEN\"}")
NEW_RT=$(jq -r .refresh_token <<<"$ROTATED")
[ "$NEW_RT" != "$REFRESH_TOKEN" ] && echo "  refresh token rotated ✓"

step "Replay the (now-rotated) original — must be 401"
HTTP=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$RELAY/auth/token/refresh" \
  -H "Content-Type: application/json" \
  -d "{\"refresh_token\":\"$REFRESH_TOKEN\"}")
echo "  HTTP $HTTP (expected 401)"
[ "$HTTP" = "401" ]

step "List active sessions"
NEW_AT=$(jq -r .access_token <<<"$ROTATED")
curl -sf "$RELAY/auth/sessions" -H "Authorization: Bearer $NEW_AT" | jq

echo
echo "All smoke checks passed."
