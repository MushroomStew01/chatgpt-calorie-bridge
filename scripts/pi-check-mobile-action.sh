#!/usr/bin/env bash
# Read-only checks. Never print credentials or meal records.
set -euo pipefail
sudo docker exec -i calorie-bridge-app python - <<'PY'
import json
import os
import urllib.error
import urllib.request

expected = 'https://stewytailscale.tail93bb57.ts.net'
public = os.environ.get('PUBLIC_BASE_URL', '').rstrip('/')
if public != expected:
    raise SystemExit('PUBLIC_BASE_URL must be the Pi HTTPS origin (without :8443). No changes made.')
key = os.environ.get('APP_API_KEY', '')
if not key or key == 'change-me':
    raise SystemExit('Configure a real APP_API_KEY in the bridge environment.')

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

opener = urllib.request.build_opener(NoRedirect)
def read(url, authenticated=False):
    headers = {'X-API-Key': key} if authenticated else {}
    with opener.open(urllib.request.Request(url, headers=headers), timeout=20) as response:
        return json.load(response)

for origin in ('http://127.0.0.1:8000', public):
    schema = read(origin + '/action-openapi.json')
    assert schema['servers'] == [{'url': expected}], 'Action schema advertises the wrong server'
    assert schema['paths']['/api/meals']['post']['operationId'] == 'logMeal'
    assert schema['paths']['/api/summary']['get']['operationId'] == 'getDailySummary'
    print('PASS: Action schema at ' + origin)
try:
    read(public + '/api/summary')
except urllib.error.HTTPError as exc:
    if exc.code != 401:
        raise SystemExit('Unexpected unauthenticated summary status: ' + str(exc.code))
else:
    raise SystemExit('FAIL: summary was accessible without authentication')
print('PASS: public API rejects unauthenticated reads')
summary = read(public + '/api/summary', authenticated=True)
assert 'calories' in summary and summary.get('timezone') == 'America/Toronto'
print('PASS: authenticated summary returned; no meal data printed')
print('Pi API checks passed. A phone photo -> saved meal test is still required.')
PY
