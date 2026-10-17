"""NFL TD Lab Phase 14: immutable pregame snapshots and conservative postgame grading.

Run from repo root: python scripts/track_predictions.py --self-test / --run
No odds data, no API tokens. Uses already-published docs/data.json, nflverse
schedule and weekly player stats. No claimed ability to verify active rosters.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc


def timestamp(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('Timestamp missing')
    obj = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if obj.tzinfo is None:
        raise ValueError('Timestamp needs a UTC offset')
    return obj.astimezone(UTC)


def iso(obj):
    return obj.astimezone(UTC).isoformat(timespec='seconds').replace('+00:00', 'Z')


def label_key(label):
    if not isinstance(label, str) or not re.fullmatch(r'[A-Z]{2,3} @ [A-Z]{2,3}', label):
        raise ValueError(f'Unsupported matchup label: {label!r}')
    return label.replace(' @ ', '_')


def season_for(kickoff):
    return kickoff.year - 1 if kickoff.month <= 3 else kickoff.year


def player_key(name):
    # Conservative: never merge distinct people by partial surname or fuzzy matching.
    return re.sub(r'[^a-z0-9]', '', str(name).casefold())


def probability(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not (0 <= value <= 1):
        raise ValueError(f'Invalid model probability {value!r}')
    return round(value, 8)


def dump_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True, allow_nan=False) + '\n'
    path.write_text(content, encoding='utf8')


def write_immutable(path, data):
    raw = (json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True, allow_nan=False) + '\n').encode('utf8')
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open('xb') as fh:
            fh.write(raw)
        return True
    except FileExistsError:
        existing = json.loads(path.read_text(encoding='utf8'))
        # The first actual archive time is immutable even when re-run later.
        if 'captured_at_utc' in existing and 'captured_at_utc' in data:
            data = {**data, 'captured_at_utc': existing['captured_at_utc']}
        if existing != data:
            raise ValueError(f'Existing immutable record has different content: {path}')
        return False


def validate_and_capture(root, now, published):
    """Capture every player's original predicted probability before kickoff.

    Keep multiple immutable snapshots per game; grading later uses the last
    archived pregame snapshot, not later live data or postgame edits.
    """
    if published.get('app_data_version') != 1 or not isinstance(published.get('games'), list):
        raise ValueError('The published game JSON has an unsupported format')
    generated = timestamp(published['generated_at_utc'])
    if generated > now + timedelta(minutes=3):
        raise ValueError('Published data claims a future creation time')
    created = 0
    skipped = 0
    for game in published['games']:
        kickoff = timestamp(game['kickoff_utc'])
        match = str(game['game'])
        slug = label_key(match)
        week = int(game['week'])
        if not 1 <= week <= 18:
            raise ValueError('NFL regular-season week outside 1..18')
        if now >= kickoff or generated >= kickoff:
            skipped += 1
            continue  # never backdate or fabricate a pregame forecast
        players = game.get('all_players')
        if not isinstance(players, list) or not players:
            raise ValueError(f'No player list to archive for {match}')
        excluded = {(p.get('team'), player_key(p.get('name'))) for p in game.get('excluded', [])}
        saved, seen = [], set()
        teams = match.split(' @ ')
        for p in players:
            name, team = p.get('name'), p.get('team')
            if not isinstance(name, str) or not name.strip() or team not in teams:
                raise ValueError(f'Bad player name/team in {match}')
            unique = (team, player_key(name))
            if unique in seen:
                raise ValueError(f'Duplicate team-player identity in {match}: {name}')
            seen.add(unique)
            saved.append({'player': name, 'team': team, 'position': str(p.get('position') or '?'),
                          'predicted_probability': probability(p.get('td_probability')),
                          'availability': str(p.get('status') or 'UNKNOWN / NOT VERIFIED'),
                          'excluded_from_favorites': unique in excluded})
        season = season_for(kickoff)
        path = (root / 'tracking' / 'snapshots' / str(season) / f'week_{week:02d}' / slug /
                (generated.strftime('%Y%m%dT%H%M%SZ') + '.json'))
        record = {'schema_version': 1, 'season': season, 'week': week, 'game': match,
                  'kickoff_utc': iso(kickoff), 'forecast_generated_at_utc': iso(generated),
                  'captured_at_utc': iso(now), 'model': 'phase7', 'players': saved,
                  'provenance': 'Published dashboard snapshot, archived by GitHub Actions before kickoff'}
        if write_immutable(path, record):
            created += 1
    return created, skipped


def snapshot_files(root):
    return sorted((root / 'tracking' / 'snapshots').glob('*/week_*/*/*.json'))


def best_snapshots(root, now):
    """One last available pregame forecast per matchup, no retroactive edits."""
    best = {}
    for path in snapshot_files(root):
        data = json.loads(path.read_text(encoding='utf8'))
        k = (data['season'], data['week'], data['game'])
        created = timestamp(data['forecast_generated_at_utc'])
        kickoff = timestamp(data['kickoff_utc'])
        captured = timestamp(data.get('captured_at_utc', data['forecast_generated_at_utc']))
        if created >= kickoff or created > now or captured >= kickoff or captured > now:
            continue
        if k not in best or created > timestamp(best[k]['forecast_generated_at_utc']):
            best[k] = data
    return best


def is_final_schedule(row, now, m):
    """Require both final scores and 6h after kickoff; do not grade live scores."""
    kickoff = m.kickoff_utc(row)
    if kickoff is None or now < kickoff + timedelta(hours=6):
        return False
    if (row.get('game_type') or 'REG').upper() != 'REG':
        return False
    try:
        home_score = float(row.get('home_score'))
        away_score = float(row.get('away_score'))
    except (ValueError, TypeError):
        return False
    return all(math.isfinite(x) and x >= 0 for x in (home_score, away_score))


def is_reg_stat(row):
    return (row.get('season_type') or row.get('game_type') or 'REG').upper() in ('REG','REGULAR')


def stat_lookup(stats, season, week, game, m):
    teams = game.split(' @ ')
    by_team = {t: [] for t in teams}
    for r in stats:
        if not is_reg_stat(r):
            continue
        try:
            row_week = int(float(r.get('week') or 0))
        except (ValueError, TypeError):
            continue
        if row_week != week:
            continue
        team = m.clean_team(r.get('team') or r.get('recent_team'))
        if team in by_team:
            by_team[team].append(r)
    # Current nflverse weekly stats are not a full active-roster feed.
    # Require both teams to have some offensive records before grading.
    if any(len(v) < 2 for v in by_team.values()):
        return None
    lookup = {}
    for team, rows in by_team.items():
        for r in rows:
            name = r.get('player_display_name') or r.get('player_name')
            if not name:
                continue
            key = (team, player_key(name))
            if key in lookup:  # ambiguous => don't guess identity/outcome
                lookup[key] = None
                continue
            tds = m.num(r,'rushing_tds') + m.num(r,'receiving_tds')
            if not math.isfinite(tds) or tds < 0:
                lookup[key] = None
                continue
            lookup[key] = int(tds > 0)
    return lookup


def grade_game(snap, stats, m, now):
    lookup = stat_lookup(stats, snap['season'], snap['week'], snap['game'], m)
    if lookup is None:
        return None
    graded = []
    for p in snap['players']:
        key = (p['team'], player_key(p['player']))
        outcome = lookup.get(key)
        graded.append({**p, 'scored_td': outcome if outcome in (0,1) else None,
                       'verification': 'WEEKLY_STAT_ROW' if outcome in (0,1) else 'UNVERIFIED_NO_UNIQUE_STAT_ROW'})
    return {'schema_version': 1, 'season': snap['season'], 'week': snap['week'], 'game': snap['game'],
            'kickoff_utc': snap['kickoff_utc'], 'forecast_generated_at_utc': snap['forecast_generated_at_utc'],
            'graded_at_utc': iso(now), 'outcome_definition': 'at least one rushing or receiving touchdown',
            'validation_note': 'Only players with a unique same-week offensive stat row are graded; others remain unverified.',
            'players': graded}


def grade_finished(root, now, schedule, load_stats, m, w):
    best = best_snapshots(root, now)
    schedule_map = {}
    for row in schedule:
        try:
            season, week = int(float(row.get('season'))), int(float(row.get('week')))
        except (ValueError, TypeError):
            continue
        label = f"{m.clean_team(row.get('away_team'))} @ {m.clean_team(row.get('home_team'))}"
        schedule_map[(season, week, label)] = row
    cached_stats, saved, pending = {}, 0, 0
    for (season, week, game), snap in sorted(best.items()):
        dest = (root / 'tracking' / 'results' / str(season) / f'week_{week:02d}' /
                (label_key(game) + '.json'))
        if dest.exists():
            continue
        match_row = schedule_map.get((season, week, game))
        if not match_row or not is_final_schedule(match_row, now, w):
            pending += 1
            continue
        if season not in cached_stats:
            try:
                cached_stats[season] = load_stats(season)
            except (OSError, RuntimeError, ValueError) as exc:
                print(f'  Stats not ready for season {season}: {exc}')
                pending += 1
                continue
        report = grade_game(snap, cached_stats[season], m, now)
        if report is None:
            pending += 1
            continue
        if write_immutable(dest, report):
            saved += 1
    return saved, pending


def make_public_summary(root, now):
    snaps = best_snapshots(root, now)
    games = []
    n_total = n_scored = n_evaluated = 0
    brier_sum = 0.0
    for path in sorted((root / 'tracking' / 'results').glob('*/week_*/*.json')):
        record = json.loads(path.read_text(encoding='utf8'))
        n_valid = 0
        g_brier = g_scored = 0.0
        for p in record['players']:
            if p['scored_td'] not in (0, 1):
                continue
            # Exclude known OUT players, but still archive their original predictions.
            if p.get('excluded_from_favorites'):
                continue
            n_valid += 1
            g_scored += p['scored_td']
            g_brier += (p['predicted_probability'] - p['scored_td'])**2
        n_total += len(record['players'])
        n_evaluated += n_valid
        n_scored += int(g_scored)
        brier_sum += g_brier
        games.append({'game': record['game'], 'season': record['season'], 'week': record['week'],
                      'forecast_generated_at_utc': record['forecast_generated_at_utc'],
                      'graded_players': n_valid, 'archived_players': len(record['players']),
                      'scorers': int(g_scored), 'brier_scored_rows': round(g_brier / n_valid, 5) if n_valid else None})
    games.sort(key=lambda x: (x['season'], x['week'], x['game']), reverse=True)
    return {'schema_version': 1, 'generated_at_utc': iso(now), 'model': 'phase7',
            'archived_games': len(snaps), 'graded_games': len(games),
            'archived_player_rows_in_graded_games': n_total,
            'graded_player_rows': n_evaluated,
            'ungraded_player_rows': n_total - n_evaluated,
            'scoring_rate_on_graded_rows': (round(n_scored/n_evaluated, 5) if n_evaluated else None),
            'brier_on_graded_rows': (round(brier_sum/n_evaluated, 5) if n_evaluated else None),
            'important_limitation': 'Graded subset requires same-week stat row and excludes flagged OUT players. This is NOT a complete active-roster evaluation; missing rows do NOT equal zero touchdowns.',
            'games': games[:80]}


def run(root=ROOT, now=None, published=None, schedule=None, load_stats=None):
    now = now or datetime.now(UTC)
    if published is None:
        f = root / 'docs' / 'data.json'
        published = json.loads(f.read_text(encoding='utf8'))
    captured, skipped = validate_and_capture(root, now, published)
    print(f'Captured {captured} new pregame game snapshots; skipped {skipped} already-started games.')
    # Fetch schedule even when no new pregame records, to grade past games.
    sys.path.insert(0, str(ROOT))
    import td_model as model
    import td_weekly as weekly
    if load_stats is None:
        load_stats = model.nfl_stats
    try:
        if schedule is None:
            schedule = weekly.load_schedule()
    except (RuntimeError, OSError, ValueError) as exc:
        print(f'  Schedule unavailable; pregame archives will still be saved: {exc}')
        schedule = []
    saved, pending = grade_finished(root, now, schedule, load_stats, model, weekly)
    summary = make_public_summary(root, now)
    out = root / 'docs' / 'forward_latest.json'
    # Do not create a misleading fresh timestamp every run if metrics unchanged?
    # Run time is transparent and archived pregame timestamp remains immutable.
    if out.exists():
        previous = json.loads(out.read_text(encoding='utf8'))
        same = {k:v for k,v in previous.items() if k != 'generated_at_utc'} == {k:v for k,v in summary.items() if k != 'generated_at_utc'}
        if same:
            summary = previous
        else:
            dump_json(out, summary)
    else:
        dump_json(out, summary)
    print(f'Graded {saved} newly finished games; {pending} awaiting verified results/stats.')
    print(f'Tracked {summary["archived_games"]} games / graded {summary["graded_games"]}; '
          f'{summary["graded_player_rows"]} graded player records; '
          f'{summary["ungraded_player_rows"]} unverified or excluded player records.')
    print(f'Updated {out}. Missing stat rows are NOT graded as zero.')
    return summary


def self_test():
    from datetime import timedelta
    sys.path.insert(0, str(ROOT))
    import td_model as model
    import td_weekly as weekly
    now = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)
    kickoff = now + timedelta(days=1)
    game = {'week':6, 'game':'BAL @ CLE', 'kickoff_utc':iso(kickoff),
            'excluded':[{'team':'BAL','name':'Demo OUT'}],
            'all_players':[{'name':'Demo Scorer','team':'BAL','position':'RB','status':'UNKNOWN','td_probability':.6},
                           {'name':'Demo Zero','team':'CLE','position':'RB','status':'UNKNOWN','td_probability':.3},
                           {'name':'Demo No Row','team':'CLE','position':'WR','status':'UNKNOWN','td_probability':.2},
                           {'name':'Demo OUT','team':'BAL','position':'WR','status':'OUT','td_probability':.9}]}
    data = {'app_data_version':1,'generated_at_utc':iso(now),'games':[game]}
    with TemporaryDirectory() as t:
        root = Path(t)
        assert validate_and_capture(root, now, data) == (1,0)
        assert validate_and_capture(root, now, data) == (0,0)
        snaps = best_snapshots(root, now)
        assert len(snaps) == 1
        assert timestamp(next(iter(snaps.values()))['captured_at_utc']) == now
        assert validate_and_capture(root, kickoff+timedelta(minutes=1), data) == (0,1)
        assert len(snapshot_files(root)) == 1
        scores = {'season':'2026','week':'6','game_type':'REG','away_team':'BAL','home_team':'CLE',
                  'gameday':'2026-10-18','gametime':'05:00','home_score':'7','away_score':'21'}
        # Use actual date/time conversion from the schedule (05:00 ET = 09:00 UTC).
        assert weekly.kickoff_utc(scores) == kickoff
        stat_rows = [
            {'week':'6','team':'BAL','player_display_name':'Demo Scorer','rushing_tds':'1','receiving_tds':'0'},
            {'week':'6','team':'BAL','player_display_name':'Demo OUT','rushing_tds':'0','receiving_tds':'0'},
            {'week':'6','team':'CLE','player_display_name':'Demo Zero','rushing_tds':'0','receiving_tds':'0'},
            {'week':'6','team':'CLE','player_display_name':'Other Starter','rushing_tds':'0','receiving_tds':'1'}]
        assert grade_finished(root, now, [scores], lambda s:stat_rows, model, weekly) == (0,1)
        assert grade_finished(root, kickoff+timedelta(hours=7), [scores], lambda s:stat_rows, model, weekly) == (1,0)
        result_file=next((root/'tracking'/'results').glob('*/week_*/*.json'))
        result=json.loads(result_file.read_text())
        outcomes={p['player']:p['scored_td'] for p in result['players']}
        assert outcomes == {'Demo Scorer':1,'Demo Zero':0,'Demo No Row':None,'Demo OUT':0}
        summary=make_public_summary(root,kickoff+timedelta(hours=7))
        assert summary['graded_player_rows']==2 and summary['ungraded_player_rows']==2
        assert abs(summary['brier_on_graded_rows'] - .125) < 1e-6
        assert grade_finished(root, kickoff+timedelta(hours=9), [scores],lambda s:stat_rows,model,weekly)==(0,0)
        data['generated_at_utc']=iso(kickoff+timedelta(seconds=1))
        assert validate_and_capture(root,kickoff+timedelta(hours=10),data)==(0,1)
    print('SELF-TEST PASS: immutable snapshots, no after-kickoff capture, real stat-row-only grading, idempotency, and public summary.')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--self-test', action='store_true')
    ap.add_argument('--run', action='store_true')
    args=ap.parse_args()
    if args.self_test:
        self_test()
    elif args.run:
        run()
    else:
        ap.error('Choose --self-test or --run')


if __name__=='__main__':
    main()
