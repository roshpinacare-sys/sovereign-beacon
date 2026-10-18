#!/usr/bin/env python3
"""beat-keepalive — the cross-repo schedule lottery.

Measured 2026-10-18 (wave-19.4/19.5): GitHub DROPS scheduled events under
load — 1 fired slot out of 7 on the executor's hourly cron, then 6/6 dropped
after the */15 density change. A single repo's cron is therefore NOT a
sovereign trigger for the beat that keeps the economy book alive.

This lane lives in a DIFFERENT repo so its schedule slots draw from an
independent drop-lottery. Each slot: read the executor's fleet-beat last-run
age via the API and dispatch the beat ONLY if it is stale (>= KEEPALIVE_STALE_MIN,
default 55min — the same threshold as the executor's own interval gate).

Laws:
- secret-surface: schedule + workflow_dispatch only; pull_request forbidden.
- fail-closed: any API error or missing credential reddens this lane with a
  clean() message — never fail open into silence.
- LEAK-LAW: every printed string passes clean(); the credential never appears.
- lean: fresh path = one API call, seconds, zero minutes. The executor's own
  interval gate makes over-waking harmless (extra slots become green skips).
"""
import base64
import json
import os
import sys
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.request import Request, urlopen

EXECUTOR = 'roshpinacare-sys/sovereign-executor'
WORKFLOW = 'fleet-beat.yml'


def clean(s):
    s = str(s)
    pat = os.environ.get('SOVEREIGN_PAT', '')
    if pat:
        s = s.replace(pat, '***REDACTED***')
    return s


def api(method, path, token, body=None):
    req = Request('https://api.github.com' + path, method=method)
    req.add_header('Authorization', 'Basic ' + base64.b64encode(
        ('x-access-token:' + token).encode()).decode())
    req.add_header('Accept', 'application/vnd.github+json')
    data = json.dumps(body).encode() if body is not None else None
    if data:
        req.add_header('Content-Type', 'application/json')
    with urlopen(req, data=data, timeout=30) as r:
        raw = r.read()
        return r.status, (json.loads(raw) if raw else None)


def main():
    tok = os.environ.get('SOVEREIGN_PAT', '')
    if not tok:
        print('keepalive: FAIL-CLOSED — no SOVEREIGN_PAT in env; cannot check or wake the beat')
        return 1
    try:
        stale_min = int(os.environ.get('KEEPALIVE_STALE_MIN', '55'))
    except ValueError:
        stale_min = 55
    try:
        st, d = api('GET', '/repos/%s/actions/workflows/%s/runs?per_page=1' % (EXECUTOR, WORKFLOW), tok)
    except HTTPError as e:
        print(clean('keepalive: FAIL-CLOSED — runs API HTTP %s' % e.code))
        return 1
    except Exception as e:
        print(clean('keepalive: FAIL-CLOSED — runs API error: %s' % e))
        return 1
    runs = (d or {}).get('workflow_runs') or []
    if not runs:
        stale, age = True, None
        print('keepalive: no fleet-beat run ever — stale by definition -> wake')
    else:
        at = datetime.fromisoformat(runs[0]['created_at'].replace('Z', '+00:00'))
        age = (datetime.now(timezone.utc) - at).total_seconds() / 60.0
        stale = age >= stale_min
        print('keepalive: last beat %.0fmin ago (threshold %d) -> %s' % (
            age, stale_min, 'STALE -> wake' if stale else 'fresh -> sleep'))
    if not stale:
        return 0
    try:
        st, _ = api('POST', '/repos/%s/actions/workflows/%s/dispatches' % (EXECUTOR, WORKFLOW),
                    tok, {'ref': 'main'})
    except HTTPError as e:
        print(clean('keepalive: FAIL-CLOSED — dispatch HTTP %s (need Actions:write on the executor)' % e.code))
        return 1
    except Exception as e:
        print(clean('keepalive: FAIL-CLOSED — dispatch error: %s' % e))
        return 1
    print('keepalive: dispatched %s — the beat wakes on free minutes' % st)
    return 0


if __name__ == '__main__':
    sys.exit(main())
