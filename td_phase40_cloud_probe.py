"""NFL TD Lab Phase 40 -- standalone, READ-ONLY cloud data-source probe.

No access to any private project files, roster, models, odds, or site.
No final NFL inactive-list authentication or game-day active claims.
The attached GitHub workflow uses workflow_dispatch ONLY: no schedule or publishing.

Windows: py td_phase40_cloud_probe.py --self-test
         py td_phase40_cloud_probe.py --preview
GitHub:  python td_phase40_cloud_probe.py --probe --season 2026 --week 6
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import ssl
import sys
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from urllib.parse import urlparse

MAX_BYTES = 24 * 1024 * 1024
MAX_ROWS = 150_000
ALLOWED_HOSTS = frozenset({
    'github.com',
    'release-assets.githubusercontent.com',
    'objects.githubusercontent.com',
})
REQUIRED_HEADERS = frozenset({'season', 'week', 'team', 'gsis_id', 'full_name', 'report_status'})
REPORT_STATUSES = frozenset({'OUT', 'DOUBTFUL', 'QUESTIONABLE'})


def valid_season_week(season: int, week: int) -> None:
    if not (2020 <= season <= 2100) or not (1 <= week <= 18):
        raise ValueError('Only plausible regular-season NFL season/week values allowed')


def injury_url(season: int) -> str:
    if not (2020 <= season <= 2100):
        raise ValueError('Invalid NFL season')
    return f'https://github.com/nflverse/nflverse-data/releases/download/injuries/injuries_{season}.csv'


def validated_https_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != 'https' or parsed.hostname not in ALLOWED_HOSTS or parsed.username or parsed.password:
        raise ValueError('HTTPS source host is not on the fixed GitHub release allowlist')


def fetch_feed(season: int) -> tuple[bytes, str]:
    origin = injury_url(season)
    validated_https_url(origin)
    req = urllib.request.Request(origin, headers={'User-Agent': 'NFL-TD-Lab-Phase40-ReadOnly/1.0'})
    ctx = ssl.create_default_context()
    # Certificate verification is MANDATORY. Do not monkeypatch SSL or set CERT_NONE.
    with urllib.request.urlopen(req, timeout=45, context=ctx) as response:
        final_url = response.geturl()
        validated_https_url(final_url)
        raw = response.read(MAX_BYTES + 1)
    if not raw or len(raw) > MAX_BYTES:
        raise ValueError('Feed is empty or exceeded the 24 MiB safety limit')
    return raw, final_url


def audit_feed(raw: bytes, season: int, week: int) -> dict:
    valid_season_week(season, week)
    if not raw or len(raw) > MAX_BYTES:
        raise ValueError('Unsafe/empty input size')
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeDecodeError as e:
        raise ValueError('Injury feed must be UTF-8 CSV') from e
    reader = csv.DictReader(io.StringIO(text, newline=''))
    if not reader.fieldnames or len(reader.fieldnames) != len(set(reader.fieldnames)):
        raise ValueError('Missing or duplicated CSV headings')
    if not REQUIRED_HEADERS <= set(reader.fieldnames):
        raise ValueError('Missing feed fields: ' + ', '.join(sorted(REQUIRED_HEADERS - set(reader.fieldnames))))

    all_rows = 0
    weekly_rows = 0
    blank_status_rows = 0
    distinct_ids = set()
    raw_counts = Counter()
    id_status = defaultdict(set)
    team_by_id = defaultdict(set)
    for row in reader:
        all_rows += 1
        if all_rows > MAX_ROWS or None in row:
            raise ValueError('Too many feed rows or malformed extra CSV columns')
        if str(row.get('season') or '').strip() != str(season):
            continue
        if str(row.get('week') or '').strip() != str(week):
            continue
        kind = str(row.get('season_type') or '').strip().upper()
        if kind and kind not in ('REG', 'REGULAR'):
            continue
        weekly_rows += 1
        pid = str(row.get('gsis_id') or '').strip()
        team = str(row.get('team') or '').strip().upper()
        status = str(row.get('report_status') or '').strip().upper()
        if not status:
            blank_status_rows += 1
            continue
        raw_counts[status] += 1
        if pid:
            distinct_ids.add(pid)
            id_status[(pid, team)].add(status)
            team_by_id[pid].add(team)
    if all_rows == 0:
        raise ValueError('CSV contains no observations')
    conflicting = sum(1 for statuses in id_status.values() if len(statuses) > 1)
    cross_team = sum(1 for teams in team_by_id.values() if len(teams) > 1)
    return {
        'all_feed_rows': all_rows,
        'target_week_rows': weekly_rows,
        'unique_ids_with_nonblank_report': len(distinct_ids),
        'blank_report_rows': blank_status_rows,
        'reported_out_rows': raw_counts['OUT'],
        'reported_questionable_rows': raw_counts['QUESTIONABLE'],
        'reported_doubtful_rows': raw_counts['DOUBTFUL'],
        'other_report_rows_require_review': sum(count for label, count in raw_counts.items() if label not in REPORT_STATUSES),
        'conflicting_id_team_report_groups': conflicting,
        'ids_with_multiple_teams': cross_team,
    }


def self_test() -> None:
    sample = ("season,week,team,gsis_id,full_name,report_status,season_type\n"
              "2026,6,HOU,GSIS1,Test Receiver,Questionable,REG\n"
              "2026,6,JAX,GSIS2,Test Back,Out,REG\n"
              "2026,6,HOU,GSIS3,Test Tight End,,REG\n"
              "2026,6,HOU,GSIS1,Test Receiver,Out,REG\n"
              "2026,5,JAX,GSIS4,Prior Week,Out,REG\n"
              "2026,6,JAX,GSIS9,Preseason Excluded,Out,PRE\n").encode()
    report = audit_feed(sample, 2026, 6)
    assert report['all_feed_rows'] == 6
    assert report['target_week_rows'] == 4
    assert report['reported_out_rows'] == 2
    assert report['reported_questionable_rows'] == 1
    assert report['conflicting_id_team_report_groups'] == 1
    assert report['blank_report_rows'] == 1
    try:
        audit_feed(b'season,week\n2026,6\n', 2026, 6)
    except ValueError:
        pass
    else:
        raise AssertionError('Missing headers accepted')
    for unsafe in ('http://github.com/a', 'https://evil.example/file', 'https://github.com.evil.example/file'):
        try:
            validated_https_url(unsafe)
        except ValueError:
            pass
        else:
            raise AssertionError('Unsafe source accepted')
    assert injury_url(2026).endswith('injuries_2026.csv')
    print('SELF-TEST PASS: offline CSV validation, weekly scoping, conflict/unknown reporting, HTTPS host rules; no network, no site/model writes.')


def preview() -> None:
    print('PHASE 40 | CLOUD CONNECTIVITY PROTOTYPE (PREVIEW ONLY)')
    print('Purpose: confirm GitHub Actions can fetch/parse public nflverse secondary injury CSV with verified TLS.')
    print('Files to put in GitHub repo:')
    print('  td_phase40_cloud_probe.py  (repository root)')
    print('  .github/workflows/phase40-cloud-probe.yml')
    print('Trigger: GitHub Actions -> Phase 40 Read-Only Injury Feed Probe -> Run workflow.')
    print('No schedule: workflow_dispatch only. No editing docs/, output/, archives, or GitHub releases.')
    print('No official inactive authentication, injury truth claims, player joins, or betting edge.')
    print('This preview makes no network request and changes no GitHub files.')


def probe(season: int, week: int) -> None:
    valid_season_week(season, week)
    start = datetime.now(timezone.utc)
    raw, resolved_url = fetch_feed(season)
    result = audit_feed(raw, season, week)
    end = datetime.now(timezone.utc)
    print(f'PHASE 40 | CLOUD SECONDARY INJURY FEED | {season} Week {week}')
    print('NETWORK: TLS certificate verified; expected GitHub release host and CSV schema checked.')
    print('Source: ' + injury_url(season))
    print('Final HTTPS host: ' + (urlparse(resolved_url).hostname or ''))
    print('Capture-start UTC: ' + start.isoformat())
    print('Capture-finish UTC: ' + end.isoformat())
    print('Source SHA256: ' + hashlib.sha256(raw).hexdigest())
    print(f'Bytes read (in memory only): {len(raw):,}')
    for name, value in result.items():
        print(f'{name}: {value}')
    if result['target_week_rows'] == 0:
        print('WARNING: No matching report rows this week; NOT evidence all players are healthy or active.')
    if result['conflicting_id_team_report_groups'] or result['ids_with_multiple_teams']:
        print('WARNING: Feed includes ambiguous player report observations; review required.')
    print('SECONDARY SOURCE ONLY. Capture time is NOT official publication time.')
    print('NO final inactive verification, player model updates, iPhone publication, or historical changes.')
    print('SUCCESS: cloud-source-only probe completed; output is console logs, not saved files.')


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Private/read-only GH Actions injury source probe')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--self-test', action='store_true')
    mode.add_argument('--preview', action='store_true')
    mode.add_argument('--probe', action='store_true')
    parser.add_argument('--season', type=int, default=2026)
    parser.add_argument('--week', type=int, default=6)
    args = parser.parse_args(argv)
    try:
        if args.self_test:
            self_test()
        elif args.preview:
            preview()
        else:
            probe(args.season, args.week)
    except (ValueError, OSError, TimeoutError, urllib.error.URLError) as e:
        print('PHASE40 ERROR: '+type(e).__name__+': '+str(e)[:500], file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
