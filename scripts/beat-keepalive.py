#!/usr/bin/env python3
"""beat-keepalive — the cross-repo schedule lottery, made self-chaining (wave-19.11).

Measured 2026-10-18 (wave-19.4/19.5): GitHub DROPS scheduled events under
load — 1 fired slot out of 7 on the executor's hourly cron, then 6/6 dropped
after the */15 density change. A single repo's cron is therefore NOT a
sovereign trigger for the beat that keeps the economy book alive.

MEASURED 2026-10-19 (wave-19.11): this lane's own */15 ticket wins only once
per ~2h — runs #2->#5 landed 17:33Z, 19:41Z, 21:54Z, 23:55Z and then a ~3h
dead gap with zero runs. The lifeline's detection latency was bounded by the
lottery, not by the design. THE DWELL-CHAIN LAW: one run now dwells inside
the runner (KEEPALIVE_CYCLES staleness checks, 15min apart = ~30min of
continuous cover per run), then hands the chain to a single successor.
ONE lottery win per hour now sustains a continuous 15-minute heart-watch;
the */15 schedule is demoted to bootstrap — it only has to win once after
any outage to re-seed the chain. Chain death (API error at handoff) is
honest: exit 1, the lottery remains.

Chain-split-proof handoff: the successor is dispatched ONLY if the newest
keepalive run is this run itself (GITHUB_RUN_ID). A schedule win that
arrived during the dwell is newer, so the older run exits without
dispatching and the newer one carries the chain. The concurrency group
serializes; the population stays at one live chain plus at most one queued
bootstrap. Public repos have unlimited free minutes — the dwell costs
sleep, not money.

LIFELINE LAW (wave-19.10): the switch-guard lives on the plane it guards —
if the executor's OWN repo-level Actions switch is flipped OFF, the hourly
guard dies with the beat and nothing self-heals. This lane is the outside
observer: when the wake dispatch fails, it probes the executor's
actions/permissions and names the true cause. A switch-OFF heart is the one
failure this fleet cannot heal autonomously — it is diagnosed loudly for the
owner, never silently mistaken for a permissions problem. (Honest boundary:
if THIS repo's switch is flipped, the lane cannot even run — the beacon lane
carries the same diagnosis as the redundant second observer.)

Laws:
- secret-surface: schedule + workflow_dispatch only; pull_request forbidden.
- fail-closed: any API error or missing credential reddens this lane with a
  clean() message — never fail open into silence. A failed handoff exits 1
  and the chain dies honestly — the */15 bootstrap remains.
- LEAK-LAW: every printed string passes clean(); the credential never appears.
- lean: a fresh check is one API call; dwell cycles cost sleep, not calls.
"""
import base64
import json
import os
import sys
import time
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.request import Request, urlopen

EXECUTOR = 'roshpinacare-sys/sovereign-executor'
WORKFLOW = 'fleet-beat.yml'
SELF_REPO = 'roshpinacare-sys/sovereign-beacon'
SELF_WF = 'keepalive.yml'


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


def diagnose_dispatch_failure(tok):
    """LIFELINE LAW (wave-19.10): name the TRUE cause of a failed wake.
    Returns the diagnostic line; the caller prints and fails closed."""
    try:
        st, perms = api('GET', '/repos/%s/actions/permissions' % EXECUTOR, tok)
        enabled = bool((perms or {}).get('enabled', True))
    except Exception as e:
        return clean('lifeline: dispatch failed AND the switch probe failed (%s) — '
                     'the heart is unreachable, owner decision required' % e)
    if st == 200 and not enabled:
        return ('lifeline: THE HEART\'S SWITCH IS OFF — executor '
                'actions/permissions.enabled=false. The beat cannot start, the '
                'hourly guard died with it (it lives on the guarded plane), and '
                'nothing self-heals. This is the one failure the fleet cannot '
                'fix autonomously — owner decision required. (Re-arm order '
                'standing: trace 1a1512554a26f66d covers the approved 18; the '
                'executor itself needs its own word.)')
    if st == 200:
        return clean('keepalive: FAIL-CLOSED — dispatch rejected (switch is ON; '
                     'likely missing Actions:write scope on the credential)')
    return clean('lifeline: dispatch failed; switch probe HTTP %s — diagnose manually' % st)


def check_beat(tok, stale_min):
    """One staleness check. Returns (ok, stale, err). One API call."""
    try:
        st, d = api('GET', '/repos/%s/actions/workflows/%s/runs?per_page=1' % (EXECUTOR, WORKFLOW), tok)
    except HTTPError as e:
        return False, False, 'keepalive: FAIL-CLOSED — runs API HTTP %s' % e.code
    except Exception as e:
        return False, False, clean('keepalive: FAIL-CLOSED — runs API error: %s' % e)
    runs = (d or {}).get('workflow_runs') or []
    if not runs:
        print('keepalive: no fleet-beat run ever — stale by definition -> wake')
        return True, True, ''
    at = datetime.fromisoformat(runs[0]['created_at'].replace('Z', '+00:00'))
    age = (datetime.now(timezone.utc) - at).total_seconds() / 60.0
    stale = age >= stale_min
    print('keepalive: last beat %.0fmin ago (threshold %d) -> %s' % (
        age, stale_min, 'STALE -> wake' if stale else 'fresh -> dwell'))
    return True, stale, ''


def wake_beat(tok):
    """Dispatch the executor's fleet-beat. LIFELINE: diagnose on failure."""
    try:
        st, _ = api('POST', '/repos/%s/actions/workflows/%s/dispatches' % (EXECUTOR, WORKFLOW),
                    tok, {'ref': 'main'})
    except HTTPError as e:
        if e.code in (403, 422):
            print(diagnose_dispatch_failure(tok))
        else:
            print(clean('keepalive: FAIL-CLOSED — dispatch HTTP %s (need Actions:write on the executor)' % e.code))
        return False
    except Exception as e:
        print(clean('keepalive: FAIL-CLOSED — dispatch error: %s' % e))
        return False
    print('keepalive: dispatched %s — the beat wakes on free minutes' % st)
    return True


def handoff(tok):
    """DWELL-CHAIN handoff: dispatch ONE successor iff the newest keepalive
    run is this run itself. Returns True when the chain is carried (by the
    successor OR by a newer run); False on honest chain death."""
    try:
        my_id = int(os.environ.get('GITHUB_RUN_ID', '0') or 0)
    except ValueError:
        my_id = 0
    if not my_id:
        print('keepalive: not in CI (no GITHUB_RUN_ID) — chain handoff skipped honestly')
        return True
    try:
        st, d = api('GET', '/repos/%s/actions/workflows/%s/runs?per_page=1' % (SELF_REPO, SELF_WF), tok)
    except Exception as e:
        print(clean('keepalive: FAIL-CLOSED — handoff self-check error: %s — the chain dies honestly; '
                    'the */15 bootstrap remains' % e))
        return False
    runs = (d or {}).get('workflow_runs') or []
    newest = runs[0]['id'] if runs else 0
    if newest != my_id:
        print('keepalive: newer keepalive run exists (id %s) — the chain is already carried, '
              'no successor dispatched' % newest)
        return True
    try:
        st, _ = api('POST', '/repos/%s/actions/workflows/%s/dispatches' % (SELF_REPO, SELF_WF),
                    tok, {'ref': 'main'})
    except HTTPError as e:
        print(clean('keepalive: FAIL-CLOSED — successor dispatch HTTP %s — the chain dies honestly; '
                    'the */15 bootstrap remains' % e.code))
        return False
    except Exception as e:
        print(clean('keepalive: FAIL-CLOSED — successor dispatch error: %s — the chain dies honestly; '
                    'the */15 bootstrap remains' % e))
        return False
    print('keepalive: chain handed to successor (dispatch HTTP %s) — the watch continues' % st)
    return True


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
        cycles = max(1, int(os.environ.get('KEEPALIVE_CYCLES', '3')))
    except ValueError:
        cycles = 3
    try:
        dwell = max(0, int(os.environ.get('KEEPALIVE_DWELL_SEC', '900')))
    except ValueError:
        dwell = 900
    for i in range(1, cycles + 1):
        print('keepalive: dwell cycle %d/%d' % (i, cycles))
        ok, stale, err = check_beat(tok, stale_min)
        if not ok:
            print(err)
            return 1
        if stale and not wake_beat(tok):
            return 1
        if i < cycles and dwell:
            time.sleep(dwell)
    return 0 if handoff(tok) else 1


if __name__ == '__main__':
    sys.exit(main())
