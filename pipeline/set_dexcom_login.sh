#!/usr/bin/env bash
# Save David's Dexcom account login (the one the G7 app uses, not a follower's) to Heath's .env,
# then test it against Dexcom Share. The password is typed hidden and never printed.
# Run from the Mac:  ssh -t scout-hermes /root/health-dashboard/pipeline/set_dexcom_login.sh
set -euo pipefail
ENV=/root/.hermes/profiles/heath/.env
read -r -p "Dexcom username or email: " U
read -r -s -p "Dexcom password: " P; echo
[ -n "$U" ] && [ -n "$P" ] || { echo "Nothing saved: username and password are both needed."; exit 1; }
TMP=$(mktemp)
grep -v -E '^DEXCOM_(USERNAME|PASSWORD)=' "$ENV" > "$TMP" || true
printf 'DEXCOM_USERNAME=%s\nDEXCOM_PASSWORD=%s\n' "$U" "$P" >> "$TMP"
cat "$TMP" > "$ENV"; rm -f "$TMP"; chmod 600 "$ENV"
unset P
echo "Saved. Testing the connection to Dexcom Share..."
if out=$(/usr/local/lib/hermes-agent/venv/bin/python /root/health-dashboard/pipeline/glucose_now.py 2>&1); then
  echo "$out" | head -2
else
  echo "Test failed. Check that Share is on in the Dexcom G7 app (Connections > Share), then run this again."
fi
