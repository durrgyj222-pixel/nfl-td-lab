"""NFL TD Lab Phase 11: independently runnable, fail-closed forecast publishing.

Run from repository checkout:
  python scripts/refresh_dashboard.py --self-test     # no internet, no real website writes
  python scripts/refresh_dashboard.py                  # automatic season, live refresh
  python scripts/refresh_dashboard.py --season 2026    # specific season

No sports-odds API or secrets required. It touches only docs/data.json after
*all* upstream commands and consistency checks succeed. Keeps old site on errors.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'output'
PUBLISHED = ROOT / 'docs' / 'data.json'


def parse_utc(value):
    if not isinstance(value, str) or not value:
        raise ValueError('Missing UTC timestamp.')
    try:
        obj = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as e:
        raise ValueError(f'Unparseable timestamp {value!r}') from e
    if obj.tzinfo is None:
        raise ValueError('Timestamp missing timezone.')
    return obj.astimezone(timezone.utc)


def verify_payload(data, *, season, week, expected_games, now=None, min_players=8):
    """Check newly generated public data before publishing (no postgame data)."""
    now = now or datetime.now(timezone.utc)
    if not isinstance(data, dict) or data.get('app_data_version') != 1:
        raise ValueError('Not a Phase 9 app-data-v1 document.')
    created = parse_utc(data.get('generated_at_utc'))
    if abs((now-created).total_seconds()) > 15 * 60:
        raise ValueError('New snapshot timestamp is stale or unexpectedly in the future.')
    games = data.get('games')
    if not isinstance(games, list) or not games:
        raise ValueError('No games in new report.')
    names = [g.get('game') if isinstance(g, dict) else None for g in games]
    if len(names) != len(set(names)) or any(not name for name in names):
        raise ValueError('Duplicate/invalid game labels.')
    if set(names) != set(expected_games):
        missing = set(expected_games)-set(names)
        added = set(names)-set(expected_games)
        raise ValueError(f'Slate does not match preflight: missing {missing}, added {added}.')
    valid_players = 0
    for game in games:
        if int(game.get('week') or 0) != week:
            raise ValueError('Mixed NFL week in new report.')
        kickoff = parse_utc(game.get('kickoff_utc'))
        if kickoff < created - timedelta(minutes=1):
            raise ValueError(f'Already-started game found in report: {game["game"]}.')
        if not isinstance(game.get('weather'), dict):
            raise ValueError('Missing weather metadata.')
        players = game.get('all_players')
        favorites = game.get('favorites')
        if not isinstance(players, list) or not isinstance(favorites, list):
            raise ValueError('Missing player lists.')
        if len(players) < min_players:
            raise ValueError(f'Only {len(players)} players for {game["game"]}: may be incomplete.')
        teams = game['game'].split(' @ ')
        if len(teams) != 2:
            raise ValueError('Game labels must contain AWAY @ HOME.')
        teams_found = set()
        for player in players:
            if not isinstance(player, dict):
                raise ValueError('Invalid player record.')
            if not player.get('name') or player.get('team') not in teams:
                raise ValueError('Missing player identity or mismatched team.')
            teams_found.add(player['team'])
            p = player.get('td_probability')
            if not isinstance(p, (int, float)) or isinstance(p, bool) or not (0.0 <= p <= 1.0):
                raise ValueError('Probability missing/outside 0–1.')
            if not isinstance(player.get('status'), str) or not player['status']:
                raise ValueError('Missing availability flag.')
        if len(teams_found) != 2:
            raise ValueError(f'Missing projections for one team in {game["game"]}.')
        valid_players += len(players)
    if valid_players > 1000:
        raise ValueError('Implausible forecast count, refusing to publish.')
    return len(games), valid_players


def atomic_publish(data, dest=PUBLISHED):
    """Never destroy previously working site data if anything fails."""
    dest = Path(dest)
    if not dest.parent.is_dir() or not (dest.parent/'index.html').exists():
        raise FileNotFoundError('docs/index.html is missing: refusing to create/publish a partial site.')
    content = (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)+'\n').encode('utf8')
    # Temporary file on same filesystem for atomic os.replace.
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix='.td-snapshot-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return dest


def run_command(*args):
    command = [sys.executable, *args]
    print(f'\n>> {" ".join(command)}', flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def preflight(season):
    sys.path.insert(0, str(ROOT))
    import td_weekly as weekly
    import td_model as model
    schedule = weekly.load_schedule()
    week, upcoming = weekly.upcoming_games(schedule, season)
    if not upcoming:
        print(f'No future regular-season NFL games in {season}. Existing website unchanged.')
        return None
    if week < 4:
        print('Not enough same-season historical usage for Phase 7 (requires at least Week 4). Site unchanged.')
        return None
    player_data = model.nfl_stats(season)
    completed_weeks = []
    for row in player_data:
        if (row.get('season_type') or row.get('game_type') or 'REG').upper() == 'REG':
            try: completed_weeks.append(int(float(row.get('week') or 0)))
            except (ValueError, TypeError): pass
    if max(completed_weeks, default=0) < week-1:
        print(f'Stats are not yet updated through week {week-1}; keeping previous dashboard.')
        return None
    print(f'Preflight: season {season}, week {week}, {len(upcoming)} upcoming games.')
    return week, {f"{g['away']} @ {g['home']}" for g in upcoming}


def live_refresh(season, train_season):
    if train_season >= season:
        raise ValueError('Training season must precede current season.')
    slate = preflight(season)
    if slate is None:
        return False
    week, expected_games = slate
    run_command('td_phase7_fixed.py', '--weekly', '--season', str(season), '--train-season', str(train_season), '--week', str(week))
    run_command('td_phase8_weather.py', '--season', str(season), '--week', str(week))
    run_command('td_phase9_availability.py', '--season', str(season), '--week', str(week))
    file = OUTPUT / f'phase9_week_{week}_{season}_games.json'
    with file.open('r', encoding='utf8') as f:
        data = json.load(f)
    counts = verify_payload(data, season=season, week=week, expected_games=expected_games)
    atomic_publish(data)
    print(f'PUBLISHED docs/data.json for NFL {season} Week {week}: {counts[0]} games, {counts[1]} players.')
    print('Site will redeploy from main/docs after GitHub Actions commits this file.')
    return True


def self_test():
    now = datetime.now(timezone.utc)
    game = {'game':'ATL @ CHI', 'week':6,
            'kickoff_utc':(now+timedelta(days=1)).isoformat(),
            'weather':{'stadium':'Fixture Field', 'weather_flags':'TEST'},
            'all_players':[], 'favorites':[]}
    for t in ('ATL', 'CHI'):
        for i in range(5):
            game['all_players'].append({'name':f'Example {t} Player {i}','team':t,
                'td_probability':0.1+i*0.05,'status':'UNKNOWN / NOT VERIFIED'})
    valid = {'app_data_version':1,'generated_at_utc':now.isoformat(), 'games':[game]}
    assert verify_payload(valid, season=2026, week=6, expected_games={'ATL @ CHI'}, now=now) == (1, 10)
    with tempfile.TemporaryDirectory() as d:
        temp=Path(d); docs=temp/'docs'; docs.mkdir(); (docs/'index.html').write_text('valid website')
        target=docs/'data.json'; target.write_text('OLD SITE DATA')
        atomic_publish(valid, target)
        assert json.loads(target.read_text())['games'][0]['game']=='ATL @ CHI'
        original=target.read_bytes()
        corrupted=json.loads(json.dumps(valid)); corrupted['games'][0]['all_players'][0]['td_probability']=1.9
        try: verify_payload(corrupted, season=2026, week=6, expected_games={'ATL @ CHI'}, now=now)
        except ValueError: pass
        else: raise AssertionError('Out-of-range probability was incorrectly accepted.')
        assert target.read_bytes() == original, 'Invalid data must not overwrite working site.'
        corrupted=json.loads(json.dumps(valid)); corrupted['games'][0]['kickoff_utc']=(now-timedelta(hours=1)).isoformat()
        try: verify_payload(corrupted, season=2026, week=6, expected_games={'ATL @ CHI'}, now=now)
        except ValueError: pass
        else: raise AssertionError('A kicked-off game was incorrectly accepted.')
        assert target.read_bytes() == original
    print('SELF-TEST PASS: weekly slate, identity, probability checks, and atomic website update.')


def main():
    parser=argparse.ArgumentParser(description='NFL TD Lab Phase 11 safe site refresh')
    parser.add_argument('--self-test', action='store_true')
    parser.add_argument('--season', type=int, default=None)
    parser.add_argument('--train-season',type=int,default=2024)
    args=parser.parse_args()
    if args.self_test:
        self_test(); return
    now=datetime.now(timezone.utc)
    season=args.season or (now.year if now.month>=7 else now.year-1)
    try:
        live_refresh(season,args.train_season)
    except (OSError,ValueError,RuntimeError,subprocess.CalledProcessError,ImportError) as exc:
        print(f'UPDATE FAILED: {exc}', file=sys.stderr)
        print('Prior docs/data.json was not replaced.', file=sys.stderr)
        raise SystemExit(1)


if __name__=='__main__':
    main()
