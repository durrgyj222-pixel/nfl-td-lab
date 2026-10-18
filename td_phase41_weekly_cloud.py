"""Phase 41: read-only, season/week-aware GitHub Actions injury-feed probe.

Uses Phase 40's TLS/CSV audit. No roster joins, final inactive claims,
model updates, files written, or site publication.

Commands:
  python td_phase41_weekly_cloud.py --self-test
  python td_phase41_weekly_cloud.py --preview
  python td_phase41_weekly_cloud.py --run
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import ssl
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import urlparse

from td_phase40_cloud_probe import (
    audit_feed, fetch_feed, injury_url,
    validated_https_url,
)

SCHEDULE_URL = 'https://github.com/nflverse/nflverse-data/releases/download/schedules/games.csv'
SCHEDULE_MAX_BYTES = 12 * 1024 * 1024
SCHEDULE_MAX_ROWS = 20_000


def fetch_schedule() -> tuple[bytes, str]:
    # Test a full valid slate and its compatible Phase 40 injury CSV audit.
    full = ("season,week,game_type,gameday,gametime,home_team,away_team\n" +
            "".join(f"2026,6,REG,2026-10-18,13:00,H{i},A{i}\n" for i in range(8))).encode()
    full_weeks, _ = parse_schedule(full)
    full_selection = resolve_week(full_weeks, utc('2026-10-18T15:00:00Z'))
    assert full_selection and full_selection['games'] >= 8
    injury = ("season,week,team,gsis_id,full_name,report_status,season_type\n"
              "2026,6,H0,gsis-1,Test Player,Questionable,REG\n"
              "2026,5,H0,gsis-2,Older Week,Out,REG\n").encode()
    audited = audit_feed(injury, full_selection['season'], full_selection['week'])
    assert audited['target_week_rows'] == 1 and audited['reported_questionable_rows'] == 1
    validated_https_url(SCHEDULE_URL)
    req = urllib.request.Request(SCHEDULE_URL,
                                 headers={'User-Agent': 'NFL-TD-Lab-Phase41-ReadOnly/1.0'})
    with urllib.request.urlopen(req, timeout=45, context=ssl.create_default_context()) as response:
        final_url = response.geturl()
        validated_https_url(final_url)
        raw = response.read(SCHEDULE_MAX_BYTES + 1)
    if not raw or len(raw) > SCHEDULE_MAX_BYTES:
        raise ValueError('Missing/oversize schedules CSV')
    return raw, final_url


def nth_sunday(year: int, month: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))


def kickoff_utc(gameday: str, gametime: str) -> datetime | None:
    """Convert documented nflverse Eastern-local kickoff into UTC.

    No made-up kickoff for TBD time. Games don't start at the 2am DST transition.
    """
    try:
        day = date.fromisoformat(str(gameday).strip()[:10])
        clock = time.fromisoformat(str(gametime).strip())
    except (TypeError, ValueError):
        return None
    if clock.tzinfo is not None:
        raise ValueError('Expected Eastern-local clock without explicit offset')
    dst = nth_sunday(day.year, 3, 2) <= day < nth_sunday(day.year, 11, 1)
    return datetime.combine(day, clock, tzinfo=timezone.utc) + timedelta(hours=4 if dst else 5)


def parse_schedule(raw: bytes) -> tuple[dict[tuple[int,int], list[dict]], int]:
    if not raw or len(raw) > SCHEDULE_MAX_BYTES:
        raise ValueError('Invalid schedule source size')
    try:
        data = raw.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise ValueError('Schedule is not valid UTF-8') from exc
    reader = csv.DictReader(io.StringIO(data, newline=''))
    if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
        raise ValueError('Missing/duplicate schedule headers')
    expected = {'season', 'week', 'game_type', 'gameday', 'gametime', 'home_team', 'away_team'}
    if not expected <= set(reader.fieldnames):
        raise ValueError('Schedule missing headers: ' + ','.join(sorted(expected - set(reader.fieldnames))))
    weeks = defaultdict(list)
    seen = set()
    parsed = 0
    for row in reader:
        parsed += 1
        if parsed > SCHEDULE_MAX_ROWS or None in row:
            raise ValueError('Schedule oversized or malformed')
        if str(row.get('game_type') or '').strip().upper() not in ('REG', 'REGULAR'):
            continue
        try:
            season = int(str(row.get('season') or '').strip())
            week = int(str(row.get('week') or '').strip())
        except ValueError:
            raise ValueError('Invalid regular-season schedule season/week')
        if not (2020 <= season <= 2100 and 1 <= week <= 18):
            continue
        home = (row.get('home_team') or '').strip().upper()
        away = (row.get('away_team') or '').strip().upper()
        if not home or not away or home == away:
            raise ValueError('Malformed regular-season matchup')
        kickoff = kickoff_utc(row.get('gameday') or '', row.get('gametime') or '')
        # Exclude TBD starts instead of inventing a kickoff.
        if kickoff is None:
            continue
        k = (season, week, home, away)
        if k in seen:
            raise ValueError('Duplicate schedule matchup')
        seen.add(k)
        weeks[(season, week)].append({'kickoff': kickoff, 'home': home, 'away': away})
    if parsed == 0 or not weeks:
        raise ValueError('No usable regular-season schedule entries')
    return dict(weeks), parsed


def resolve_week(weeks: dict, now: datetime) -> dict | None:
    if now.tzinfo is None:
        raise ValueError('Now must be timezone-aware')
    now = now.astimezone(timezone.utc)
    possible = []
    for (season, week), games in weeks.items():
        if not games:
            continue
        first = min(g['kickoff'] for g in games)
        last = max(g['kickoff'] for g in games)
        # Thursday through Monday slate, plus a short run-up and wrap-up.
        if first - timedelta(hours=48) <= now <= last + timedelta(hours=8):
            possible.append({'season': season, 'week': week, 'first': first,
                             'last': last, 'games': len(games),
                             'upcoming': sum(g['kickoff'] > now for g in games)})
    if len(possible) > 1:
        raise ValueError('Ambiguous overlapping NFL week windows; declining to guess')
    return possible[0] if possible else None


def self_test():
    sample = ("season,week,game_type,gameday,gametime,home_team,away_team\n"
              "2026,5,REG,2026-10-11,13:00,NYG,NO\n"
              "2026,6,REG,2026-10-15,20:15,DEN,SEA\n"
              "2026,6,REG,2026-10-18,09:30,JAX,HOU\n"
              "2026,6,REG,2026-10-18,13:00,CLE,BAL\n"
              "2026,6,REG,2026-10-19,20:15,SF,WAS\n"
              "2026,7,REG,2026-10-22,20:15,CHI,NE\n"
              "2026,6,PRE,2026-10-18,11:00,AAA,BBB\n").encode()
    weeks, count = parse_schedule(sample)
    assert count == 7
    assert len(weeks[(2026,6)]) == 4
    assert kickoff_utc('2026-10-18', '09:30').isoformat() == '2026-10-18T13:30:00+00:00'
    assert kickoff_utc('2026-12-06', '13:00').isoformat() == '2026-12-06T18:00:00+00:00'
    assert kickoff_utc('2026-10-18', 'TBD') is None
    utc = lambda x: datetime.fromisoformat(x.replace('Z','+00:00'))
    a = resolve_week(weeks, utc('2026-10-18T14:48:00Z'))
    assert a and (a['season'],a['week'],a['games'],a['upcoming']) == (2026,6,4,2)
    assert resolve_week(weeks, utc('2026-12-01T14:48:00Z')) is None
    b = resolve_week(weeks, utc('2026-10-22T13:00:00Z'))
    assert b and (b['season'], b['week']) == (2026,7)
    assert resolve_week(weeks, utc('2027-04-01T13:00:00Z')) is None
    assert resolve_week(weeks, utc('2026-10-20T21:00:00Z')) is None
    ambiguous = dict(weeks)
    ambiguous[(2026,7)] = [{'kickoff': utc('2026-10-18T17:00:00Z'), 'home':'FAKE','away':'NO'}]
    try:
        resolve_week(ambiguous, utc('2026-10-18T14:48:00Z'))
    except ValueError:
        pass
    else:
        raise AssertionError('Overlapping weeks not rejected')
    try:
        parse_schedule(sample + b'2026,6,REG,2026-10-18,13:00,CLE,BAL\n')
    except ValueError:
        pass
    else:
        raise AssertionError('Duplicate matchup not rejected')
    # Reuse existing Phase 40 tests to protect TLS host rules and injury CSV schema.
    # Test a full valid slate and its compatible Phase 40 injury CSV audit.
    full = ("season,week,game_type,gameday,gametime,home_team,away_team\n" +
            "".join(f"2026,6,REG,2026-10-18,13:00,H{i},A{i}\n" for i in range(8))).encode()
    full_weeks, _ = parse_schedule(full)
    full_selection = resolve_week(full_weeks, utc('2026-10-18T15:00:00Z'))
    assert full_selection and full_selection['games'] >= 8
    injury = ("season,week,team,gsis_id,full_name,report_status,season_type\n"
              "2026,6,H0,gsis-1,Test Player,Questionable,REG\n"
              "2026,5,H0,gsis-2,Older Week,Out,REG\n").encode()
    audited = audit_feed(injury, full_selection['season'], full_selection['week'])
    assert audited['target_week_rows'] == 1 and audited['reported_questionable_rows'] == 1
    validated_https_url(SCHEDULE_URL)
    try:
        validated_https_url('https://example.com/data.csv')
    except ValueError:
        pass
    else:
        raise AssertionError('Unexpected host accepted')
    print('SELF-TEST PASS: automatic weeks, offseason skip, DST, TBD times, duplicate/overlap rejection, fixed HTTPS hosts.')


def preview():
    print('PHASE 41 | AUTOMATIC NFL WEEK CLOUD PROBE -- PREVIEW ONLY')
    print('Automatic week source: verified-HTTPS nflverse games.csv (regular season only).')
    print('Select week only during its first kickoff -48h through last kickoff +8h.')
    print('No suitable week (e.g. offseason) -> SKIP; overlapping windows -> ERROR.')
    print('Fetch secondary injuries only for the selected week; report row counts and hashes.')
    print('Cloud workflow: .github/workflows/phase41-weekly-cloud.yml')
    print('Scheduled UTC: 12:00, 15:00, 18:00, 22:00 on Thu/Sat/Sun/Mon.')
    print('GH scheduled starts may be delayed/dropped; not official inactive or game-time assurance.')
    print('NO private roster, official inactives, model update, GitHub write, or published site change.')


def run(schedule_csv: str | None = None, now: datetime | None = None):
    now = now or datetime.now(timezone.utc)
    if schedule_csv:
        from pathlib import Path
        raw = Path(schedule_csv).read_bytes()
        final_url = 'USER-PROVIDED FILE (NOT VERIFIED PUBLIC SOURCE)'
    else:
        raw, final_url = fetch_schedule()
    weeks, total = parse_schedule(raw)
    current = resolve_week(weeks, now)
    print('PHASE 41 | READ-ONLY AUTOMATIC CLOUD INJURY PROBE')
    print('Capture UTC: ' + now.astimezone(timezone.utc).isoformat())
    print('Schedule source: ' + SCHEDULE_URL if not schedule_csv else 'Schedule: user-provided local CSV')
    print('Schedule resolved URL: ' + final_url)
    print('Schedule SHA256: ' + hashlib.sha256(raw).hexdigest())
    print('Schedule rows: ' + str(total))
    if current is None:
        print('NO CURRENT REGULAR-SEASON WEEK WINDOW: SKIP. No injury-feed download or file output.')
        return
    season, week = current['season'], current['week']
    print(f'Selected: {season} Week {week} | schedule games: {current["games"]} | still upcoming: {current["upcoming"]}')
    if current['games'] < 8:
        # Protect against inadvertently using truncated or partially uploaded schedules.
        raise ValueError('Week has fewer than 8 scheduled games; declining to probe partial schedule')
    feed, feed_final = fetch_feed(season)
    metrics = audit_feed(feed, season, week)
    print('Feed source: ' + injury_url(season))
    print('Feed resolved host: ' + str(urlparse(feed_final).hostname))
    print('Feed SHA256: ' + hashlib.sha256(feed).hexdigest())
    for k,v in metrics.items():
        print(f'{k}: {v}')
    if metrics['target_week_rows'] == 0:
        print('WARNING: No report rows for this week; all player report statuses UNKNOWN.')
    if metrics['conflicting_id_team_report_groups'] or metrics['ids_with_multiple_teams']:
        print('WARNING: Secondary report conflicts need manual review.')
    print('SECONDARY SOURCE ONLY; no official inactive-list authentication, player ID joins, or betting edge.')
    print('READ-ONLY: no files, site, forecasts, archives, or GitHub commits changed.')
    print('SUCCESS: automatic-week read-only cloud probe completed.')


def main(argv=None):
    p=argparse.ArgumentParser(description='Read-only schedule-aware injury cloud probe')
    modes=p.add_mutually_exclusive_group(required=True)
    modes.add_argument('--self-test',action='store_true')
    modes.add_argument('--preview',action='store_true')
    modes.add_argument('--run',action='store_true')
    p.add_argument('--schedule-csv',help='For offline fixture integration only')
    args=p.parse_args(argv)
    try:
        if args.self_test: self_test()
        elif args.preview: preview()
        else: run(schedule_csv=args.schedule_csv)
    except (ValueError, OSError, TimeoutError, urllib.error.URLError) as e:
        print('PHASE41 ERROR: ' + type(e).__name__ + ': ' + str(e)[:400],file=sys.stderr)
        return 2
    return 0


if __name__=='__main__':
    sys.exit(main())
