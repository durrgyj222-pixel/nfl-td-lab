"""NFL TD Probability Lab — Phase 3: walk-forward validation.

Runs the EXISTING, UNCALIBRATED td_model.py on historical NFL weeks, using
only stats available before each evaluated week. Assesses whether predicted
probabilities agree with historical anytime rushing/receiving TD outcomes.

Requirements: td_model.py and td_weekly.py beside this file. Python stdlib.

Examples:
    py td_backtest.py --demo
    py td_backtest.py --season 2025
    py td_backtest.py --season 2026 --start-week 4

Educational analytics only. Does not include betting odds or live prop picks.
"""

import argparse
import csv
import math
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import td_model as m
import td_weekly as weekly

HERE = Path(__file__).resolve().parent
OUT = HERE / 'output'


def regular(row):
    return (row.get('season_type') or row.get('game_type') or 'REG').upper() in {'REG', 'REGULAR'}


def row_week(row):
    try:
        return int(float(row.get('week') or 0))
    except (ValueError, TypeError):
        return 0


def actual_index(stats):
    """Index observed rushing or receiving TD; no passing TD credit.

    All player names in weekly data are normalized consistently with the
    forecasting model. Missing week rows are zero TD, not proof of activity.
    """
    values = defaultdict(float)
    rows_seen = set()
    team_weeks = set()
    for r in stats:
        if not regular(r):
            continue
        w = row_week(r)
        team = m.clean_team(r.get('team') or r.get('recent_team'))
        name = r.get('player_display_name') or r.get('player_name')
        if not (w > 0 and team and name):
            continue
        key = (w, team, m.clean_name(name))
        values[key] += m.num(r, 'rushing_tds') + m.num(r, 'receiving_tds')
        rows_seen.add(key)
        team_weeks.add((w, team))
    return values, rows_seen, team_weeks


def completed_slate_by_week(schedule, season, available_team_weeks, now):
    """Do not grade future or partially recorded weeks.

    A week is complete only when every scheduled game is >6 hours old and
    player stat rows exist for BOTH scheduled teams in every game.
    """
    all_games = defaultdict(list)
    for row in schedule:
        try:
            s = int(float(row.get('season') or 0))
        except (ValueError, TypeError):
            continue
        if s != season or (row.get('game_type') or 'REG').upper() != 'REG':
            continue
        w = row_week(row)
        home = m.clean_team(row.get('home_team'))
        away = m.clean_team(row.get('away_team'))
        start = weekly.kickoff_utc(row)
        if w and home and away and start:
            all_games[w].append((home, away, start))
    complete = {}
    for week, games in all_games.items():
        if any(start + timedelta(hours=6) >= now for _, _, start in games):
            continue
        if any((week, home) not in available_team_weeks or (week, away) not in available_team_weeks
               for home, away, _ in games):
            continue
        team_opp = {}
        for home, away, _ in games:
            team_opp[home] = away
            team_opp[away] = home
        complete[week] = team_opp
    return complete


def demo_schedule():
    # Construct a completed fictional six-week season to verify the pipeline.
    games = []
    for week in range(1, 7):
        day = datetime(2026, 9, 1) + timedelta(days=(week - 1) * 7)
        games.append({'season':'2026', 'game_type':'REG', 'week':str(week),
                      'gameday': day.date().isoformat(), 'gametime':'13:00',
                      'home_team':'PIT', 'away_team':'CLE'})
    return games


def predictions_for_week(stats, week, opponents):
    predictions = m.team_and_player_rates(stats, through_week=week - 1)
    last_teams = weekly.current_team_for_player(stats, week - 1)
    result = []
    unique = set()
    for p in predictions:
        if p['team'] not in opponents:
            continue
        if last_teams.get(p['player_key'], p['team']) != p['team']:
            continue
        key = (p['team'], p['player_key'])
        if key in unique:
            continue  # Colliding player identifiers; avoid counting twice.
        unique.add(key)
        result.append((p, opponents[p['team']]))
    return result


def grade(stats, completed, start_week=4, end_week=18):
    actuals, rows_seen, _ = actual_index(stats)
    grades = []
    for week in sorted(w for w in completed if start_week <= w <= end_week):
        try:
            forecast = predictions_for_week(stats, week, completed[week])
        except RuntimeError as e:
            print(f'Week {week}: no forecast ({e})')
            continue
        for p, opponent in forecast:
            lookup = (week, p['team'], p['player_key'])
            prob = p['model_probability']
            if not math.isfinite(prob) or not 0 <= prob <= 1:
                continue
            td_total = actuals.get(lookup, 0)
            scored = int(td_total > 0)
            grades.append({
                'week': week,
                'player': p['player'],
                'team': p['team'],
                'opponent': opponent,
                'position': p['position'],
                'predicted_td_probability': round(prob, 6),
                'scored_rush_or_receiving_td': scored,
                'rushing_or_receiving_tds': int(td_total),
                'had_player_stats_row': int(lookup in rows_seen),
                'squared_error': round((prob - scored)**2, 6),
            })
        print(f'  Week {week:>2}: {len(forecast):>3} pregame player forecasts graded')
    return grades


def reliability(rows):
    groups = defaultdict(list)
    for r in rows:
        # Buckets 0-20%, 20-40%, ..., 80-100%.
        prob = r['predicted_td_probability']
        b = min(int(prob * 5), 4)
        groups[b].append(r)
    result = []
    for bucket in range(5):
        cohort = groups.get(bucket, [])
        if not cohort:
            continue
        count = len(cohort)
        predicted = sum(x['predicted_td_probability'] for x in cohort) / count
        observed = sum(x['scored_rush_or_receiving_td'] for x in cohort) / count
        result.append({
            'probability_bucket': f'{bucket * 20}-{(bucket + 1) * 20}%',
            'forecasts': count,
            'average_prediction': round(predicted, 6),
            'actual_td_rate': round(observed, 6),
            'gap_percentage_points': round((predicted - observed) * 100, 2),
        })
    return result


def save_rows(rows, filename, fields):
    OUT.mkdir(exist_ok=True)
    dest = OUT / filename
    with dest.open('w', encoding='utf-8-sig', newline='') as handle:
        w = csv.DictWriter(handle, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    print(f'Saved: {dest}')


def main(argv=None):
    ap = argparse.ArgumentParser(description='NFL TD Lab Phase 3: walk-forward historical backtest')
    ap.add_argument('--season', type=int, default=2025,
                    help='Historical NFL season; 2025 is a useful fully completed sample')
    ap.add_argument('--start-week', type=int, default=4)
    ap.add_argument('--end-week', type=int, default=18)
    ap.add_argument('--stats-csv', help='Optional nflverse-format local CSV')
    ap.add_argument('--schedule-csv', help='Optional nflverse-format local schedule CSV')
    ap.add_argument('--demo', action='store_true', help='Test with fictional demo data; no downloads')
    args = ap.parse_args(argv)
    if not 2 <= args.start_week <= args.end_week <= 18:
        raise RuntimeError('Use weeks 2-18, with start-week <= end-week.')
    if args.demo:
        args.season = 2026
        args.start_week = 3
        args.end_week = 6
        print('DEMO MODE — FICTIONAL data; not evidence of model performance.\n')
        stats = m.nfl_stats(args.season, HERE / 'demo_stats.csv')
        schedule = demo_schedule()
    else:
        stats = m.nfl_stats(args.season, args.stats_csv)
        schedule = weekly.load_schedule(args.schedule_csv)
    _, _, available_teams = actual_index(stats)
    completed = completed_slate_by_week(schedule, args.season, available_teams,
                                        datetime.now(timezone.utc))
    weeks = [w for w in sorted(completed) if args.start_week <= w <= args.end_week]
    if not weeks:
        raise RuntimeError('No completed, fully recorded NFL weeks in your requested range.')
    print(f'\nWalk-forward backtest: season {args.season}, weeks {weeks[0]}-{weeks[-1]}')
    print('Each week predicts from PRIOR weeks only. No prices or game results feed predictions.\n')
    rows = grade(stats, completed, args.start_week, args.end_week)
    if not rows:
        raise RuntimeError('No predictions scored. Check schedule and stats team codes.')
    n = len(rows)
    avg_p = sum(r['predicted_td_probability'] for r in rows) / n
    actual_p = sum(r['scored_rush_or_receiving_td'] for r in rows) / n
    brier = sum(r['squared_error'] for r in rows) / n
    missing_rows = sum(1 for r in rows if not r['had_player_stats_row'])
    print('\nMODEL VALIDATION SUMMARY')
    print(f'  Weekly player predictions: {n:,}')
    print(f'  Average predicted TD chance: {avg_p:.1%}')
    print(f'  Actual TD scoring frequency: {actual_p:.1%}')
    print(f'  Calibration difference:       {(avg_p - actual_p) * 100:+.1f} percentage points')
    print(f'  Brier score (lower better):  {brier:.4f}')
    print(f'  Forecasts without a same-week stat row: {missing_rows} ({missing_rows/n:.1%})')
    print('\nCALIBRATION BY PROBABILITY RANGE')
    print(f"{'Model bucket':<15}{'Players':>10}{'Avg model':>13}{'Scored':>11}{'Gap':>10}")
    buckets = reliability(rows)
    for row in buckets:
        print(f"{row['probability_bucket']:<15}{row['forecasts']:>10}"
              f"{row['average_prediction']:>12.1%}{row['actual_td_rate']:>11.1%}"
              f"{row['gap_percentage_points']:>+9.1f}pp")
    save_rows(rows, f'backtest_{args.season}_predictions.csv',
              ['week', 'player', 'team', 'opponent', 'position', 'predicted_td_probability',
               'scored_rush_or_receiving_td', 'rushing_or_receiving_tds',
               'had_player_stats_row', 'squared_error'])
    save_rows(buckets, f'backtest_{args.season}_calibration.csv',
              ['probability_bucket', 'forecasts', 'average_prediction', 'actual_td_rate',
               'gap_percentage_points'])
    print('\nIMPORTANT: historical player eligibility comes from previous recorded usage,')
    print('not injury reports. Players with no actual stat row count as no TD.')
    print('Only rushing and receiving TDs are labeled; return/recovery TDs are omitted.')
    print('Historical results are not proof of profitable predictions or a betting edge.')
    if args.demo:
        print('FICTIONAL DEMO — do not interpret reported accuracy as real-world performance.')


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, ValueError, csv.Error) as error:
        print(f'\nERROR: {error}')
        sys.exit(1)
