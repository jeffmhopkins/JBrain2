#!/usr/bin/env bash
# Assert the egress proxy's deny matrix against a RUNNING proxy (docs/plans/BROWSER_AGENT_PLAN.md
# §2): every internal destination must come back 403 from Squid itself, and an ordinary public
# site must not. CI runs this against the freshly built image; it needs only curl.
#
#   deploy/egress/fence-check.sh http://127.0.0.1:3128
set -u
PROXY="${1:?usage: fence-check.sh <proxy-url>}"
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy NO_PROXY no_proxy
fail=0

denied=(
  http://169.254.169.254/latest/meta-data
  http://10.0.0.1/
  http://172.17.0.1/
  http://192.168.1.1/
  http://100.64.0.1/
  http://127.0.0.1:8000/
  http://db:5432/
  http://searxng:8080/
  http://api:8000/api/healthz
  http://localhost/
  http://jbrain.local/
  http://foo.localhost/
  http://localtest.me/
  "http://[::1]/"
  "http://[::ffff:10.0.0.1]/"
  http://2130706433/
  http://example.com:8080/
)
for url in "${denied[@]}"; do
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 15 -x "$PROXY" "$url")
  if [ "$code" = "403" ]; then echo "ok   403 $url"; else echo "FAIL $code $url"; fail=1; fi
done

# CONNECT (https) to an internal target must be refused at the tunnel, not passed through.
for url in https://169.254.169.254/ https://db/ https://localtest.me/; do
  line=$(curl -sv -o /dev/null -m 15 -x "$PROXY" "$url" 2>&1 | grep -m1 -E '^< HTTP' || true)
  case "$line" in
    *" 403"*) echo "ok   403 CONNECT $url" ;;
    *) echo "FAIL CONNECT $url: ${line:-no response}"; fail=1 ;;
  esac
done

# And the proxy is not simply closed: a public site goes through.
code=$(curl -s -o /dev/null -w '%{http_code}' -m 20 --retry 3 -x "$PROXY" http://example.com/)
if [ "$code" = "200" ]; then echo "ok   200 http://example.com/"; else echo "FAIL $code http://example.com/ (public site blocked)"; fail=1; fi

exit "$fail"
