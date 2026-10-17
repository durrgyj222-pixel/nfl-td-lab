"""NFL TD Lab Phase 5: past-season-trained probability recalibration.

Put this file beside td_model.py, td_weekly.py, and td_backtest.py.
No extra packages; uses the existing (certifi-fixed) download functions.

Commands:
  py td_phase5.py --self-test
  py td_phase5.py --evaluate --train-season 2024 --test-season 2025
  py td_phase5.py --weekly --season 2026 --train-season 2024

Design principles:
* For every historical forecast, all PREDICTORS use strictly earlier weeks.
* Only the TRAINING season's outcomes fit the recalibration model.
* The evaluation season stays untouched until scoring.
* A missing same-week player-stat row is not evidence of being inactive.
* Weekly output retains BOTH raw and adjusted probabilities, not alleged edges.
* Player availability is inferred from RECORDED USAGE, not verified rosters.
* No odds API calls or keys required.
"""
import argparse
import csv
import math
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import td_model as model
import td_weekly as weekly
import td_backtest as bt

HERE = Path(__file__).resolve().parent
OUT = HERE / 'output'

FEATURES = ('intercept', 'raw_logit', 'rb', 'wr', 'te', 'qb',
            'missed_team_games', 'recent_row_fraction', 'recent_touch_load',
            'career_row_fraction')


def sigmoid(z):
    if z >= 0:
        a = math.exp(-min(z, 700))
        return 1 / (1 + a)
    a = math.exp(max(z, -700))
    return a / (1 + a)


def clip(x, lo, hi):
    return min(hi, max(lo, x))


def team_player_history(stats):
    """Indexes actual game rows; future weeks remain in index but are NEVER read
    by feature extraction for a given target week.
    """
    team_games = defaultdict(set)
    players = defaultdict(lambda: defaultdict(float))
    seen = defaultdict(set)
    for r in stats:
        if not bt.regular(r):
            continue
        w = bt.row_week(r)
        team = model.clean_team(r.get('team') or r.get('recent_team'))
        name = r.get('player_display_name') or r.get('player_name')
        if not (w > 0 and team and name):
            continue
        key = (team, model.clean_name(name))
        team_games[team].add(w)
        seen[key].add(w)
        players[key][w] += model.num(r, 'carries') + model.num(r, 'targets')
    return team_games, players, seen


def feature_vector(p, week, history):
    """Only data recorded in weeks strictly prior to `week`. Bye-safe gap."""
    team_games, touches, seen = history
    prior_team_weeks = sorted(x for x in team_games.get(p['team'], ()) if x < week)
    player_key = (p['team'], p['player_key'])
    prior_player_weeks = {x for x in seen.get(player_key, ()) if x < week}
    last_three = prior_team_weeks[-3:]
    missed = 0
    for w in reversed(prior_team_weeks):
        if w in prior_player_weeks:
            break
        missed += 1
    recent_presence = (sum(w in prior_player_weeks for w in last_three) /
                       max(1, len(last_three)))
    avg_recent_touches = sum(touches[player_key].get(w, 0.0) for w in last_three) / max(1, len(last_three))
    season_presence = len(prior_player_weeks) / max(1, len(prior_team_weeks))
    raw = clip(float(p['model_probability']), 0.005, 0.995)
    position = p['position'].upper()
    values = (1.0, math.log(raw / (1 - raw)),
              float(position == 'RB'), float(position == 'WR'),
              float(position == 'TE'), float(position == 'QB'),
              clip(missed / 3.0, 0, 2), recent_presence,
              math.log1p(avg_recent_touches) / 3.0, season_presence)
    assert len(values) == len(FEATURES)
    return values, {'missed_team_games': missed,
                    'recorded_in_recent_3': round(recent_presence, 3),
                    'avg_recent_touches': round(avg_recent_touches, 2)}


def pregame_players(stats, week, opponents, history=None):
    history = history or team_player_history(stats)
    predictions = bt.predictions_for_week(stats, week, opponents)
    results = []
    for p, opponent in predictions:
        vector, details = feature_vector(p, week, history)
        results.append({
            'week': week, 'player': p['player'], 'team': p['team'],
            'opponent': opponent, 'position': p['position'],
            'raw_probability': float(p['model_probability']),
            'features': vector, 'model_metadata': details,
        })
    return results


def history_rows(stats, schedule, season, start_week=4, end_week=18):
    outcomes, has_row, team_weeks = bt.actual_index(stats)
    complete = bt.completed_slate_by_week(schedule, season, team_weeks, datetime.now(timezone.utc))
    history = team_player_history(stats)
    rows = []
    for w in sorted(k for k in complete if start_week <= k <= end_week):
        forecasts = pregame_players(stats, w, complete[w], history)
        for r in forecasts:
            key = (w, r['team'], model.clean_name(r['player']))
            r['scored'] = int(outcomes.get(key, 0) > 0)
            r['had_same_week_row'] = int(key in has_row)
        rows.extend(forecasts)
        print(f'  Week {w:2d}: {len(forecasts):3d} pregame forecasts')
    return rows


def solve(A, b):
    """Dense Gaussian elimination with pivoting, 10x10 in practice."""
    n = len(b)
    aug = [list(A[i]) + [b[i]] for i in range(n)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot][col]) < 1e-11:
            raise RuntimeError('Calibration system singular; not enough training data.')
        aug[col], aug[pivot] = aug[pivot], aug[col]
        scale = aug[col][col]
        for j in range(col, n + 1):
            aug[col][j] /= scale
        for r in range(n):
            if r == col:
                continue
            factor = aug[r][col]
            if factor:
                for j in range(col, n + 1):
                    aug[r][j] -= factor * aug[col][j]
    return [aug[i][n] for i in range(n)]


def objective(rows, weights, penalty):
    total = 0.0
    for r in rows:
        p = clip(sigmoid(sum(a*b for a, b in zip(r['features'], weights))), 1e-12, 1-1e-12)
        y = r['scored']
        total -= y*math.log(p) + (1-y)*math.log1p(-p)
    return total + penalty * sum(w*w for w in weights[1:]) / 2


def fit_logistic(rows, penalty=12.0, max_iter=45):
    """Ridge-regularized logistic calibration fitted on one historical season.

    Intercept is unpenalized. Newton/IRLS uses damped backtracking.
    """
    if len(rows) < 150 or sum(r['scored'] for r in rows) < 15:
        raise RuntimeError('Insufficient historical forecasts to train; need >=150 forecasts and >=15 TDs.')
    n = len(FEATURES)
    prevalence = (sum(r['scored'] for r in rows) + 0.5) / (len(rows) + 1)
    weights = [math.log(prevalence / (1-prevalence))] + [0.0] * (n-1)
    # A useful nontrivial starting point, though this is refitted on training data.
    weights[1] = 0.8
    for _ in range(max_iter):
        gradient = [0.0] * n
        hessian = [[0.0] * n for _ in range(n)]
        for r in rows:
            x = r['features']
            z = sum(weights[j] * x[j] for j in range(n))
            p = sigmoid(z)
            delta = p - r['scored']
            curvature = max(p * (1-p), 1e-8)
            for j in range(n):
                gradient[j] += delta*x[j]
                for k in range(j+1):
                    hessian[j][k] += curvature*x[j]*x[k]
        for j in range(n):
            if j:
                gradient[j] += penalty * weights[j]
                hessian[j][j] += penalty
            for k in range(j):
                hessian[k][j] = hessian[j][k]
        step = solve(hessian, gradient)
        old_obj = objective(rows, weights, penalty)
        alpha = 1.0
        while alpha > 1/1024:
            proposed = [weights[j] - alpha*step[j] for j in range(n)]
            if objective(rows, proposed, penalty) <= old_obj + 1e-8:
                break
            alpha /= 2
        if alpha <= 1/1024:
            break
        weights = proposed
        if max(abs(alpha*x) for x in step) < 1e-6:
            break
    return weights


def calibrated_probability(r, weights):
    return sigmoid(sum(x*w for x, w in zip(r['features'], weights)))


def metrics(rows, key):
    if not rows:
        return None
    n = len(rows)
    return {'n': n, 'mean_prediction': sum(r[key] for r in rows)/n,
            'actual': sum(r['scored'] for r in rows)/n,
            'brier': sum((r[key]-r['scored'])**2 for r in rows)/n,
            'log_loss': -sum(r['scored']*math.log(clip(r[key], 1e-12, 1-1e-12))+
                            (1-r['scored'])*math.log1p(-clip(r[key], 1e-12, 1-1e-12))
                            for r in rows)/n}


def show_metrics(name, met):
    print(f"  {name:<24}{met['mean_prediction']:>8.1%}{met['actual']:>10.1%}"
          f"{met['brier']:>12.4f}{met['log_loss']:>13.4f}")


def write_csv(filename, rows, fields):
    OUT.mkdir(exist_ok=True)
    path = OUT / filename
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)
    print(f'Saved: {path}')
    return path


def evaluate(train_season, test_season, schedule, train_stats, test_stats):
    if train_season >= test_season:
        raise RuntimeError('Training year must precede evaluation year to prevent time leakage.')
    print(f'\nBUILDING TRAINING FORECASTS: {train_season} (weeks 4-18)')
    train = history_rows(train_stats, schedule, train_season)
    print(f'\nBUILDING UNSEEN EVALUATION FORECASTS: {test_season} (weeks 4-18)')
    test = history_rows(test_stats, schedule, test_season)
    if not test:
        raise RuntimeError(f'No completed {test_season} weeks to evaluate.')
    weights = fit_logistic(train)
    train_prev = sum(r['scored'] for r in train)/len(train)
    for r in test:
        r['adjusted_probability'] = calibrated_probability(r, weights)
        r['train_constant_probability'] = train_prev

    print(f'\nOUT-OF-SAMPLE TEST — TRAIN {train_season}, EVALUATE {test_season}')
    print(f'  Training forecasts: {len(train):,}; unseen test forecasts: {len(test):,}')
    print(f"  {'Model':<24}{'Avg P':>8}{'Actual':>10}{'Brier':>12}{'Log loss':>13}")
    print('  ' + '-'*67)
    raw = metrics(test, 'raw_probability')
    adjusted = metrics(test, 'adjusted_probability')
    constant = metrics(test, 'train_constant_probability')
    show_metrics('Original', raw)
    show_metrics('Adjusted candidate', adjusted)
    show_metrics('2024 constant baseline' if train_season == 2024 else 'Train-season constant', constant)
    delta = raw['brier'] - adjusted['brier']
    print(f'  Brier improvement vs original: {delta:+.4f} (positive means better)')
    print('\n  HIGH-PROBABILITY COHORT (ORIGINAL PROB >=40%)')
    high = [r for r in test if r['raw_probability'] >= .4]
    if high:
        show_metrics('Original', metrics(high, 'raw_probability'))
        show_metrics('Adjusted candidate', metrics(high, 'adjusted_probability'))
    print('\n  PLAYER STAT-ROW AUDIT (diagnostic: uses postgame information)')
    for flag in (1, 0):
        cohort = [r for r in test if r['had_same_week_row'] == flag]
        print(f'  Same-week stat row = {flag}: {len(cohort):,} cases', end='')
        if cohort:
            print(f" | actual TD rate {sum(x['scored'] for x in cohort)/len(cohort):.1%}")
        else:
            print()
    export = []
    for r in test:
        export.append({k: r[k] for k in ('week','player','team','opponent','position',
                                         'raw_probability','adjusted_probability',
                                         'scored','had_same_week_row')})
    write_csv(f'phase5_{test_season}_validation.csv', export,
              ['week','player','team','opponent','position','raw_probability',
               'adjusted_probability','scored','had_same_week_row'])
    write_csv(f'phase5_{train_season}_weights.csv',
              [{'feature': name, 'coefficient': round(w, 8)} for name, w in zip(FEATURES, weights)],
              ['feature','coefficient'])
    print('\nNotes: player recorded-stat-row presence is not the same as official active status.')
    print('No sportsbook odds, goal-line touches, verified injuries, or weather are included yet.')
    if adjusted['brier'] < raw['brier'] and adjusted['log_loss'] < raw['log_loss']:
        print('RESULT: Candidate improved BOTH evaluated probability metrics on this test season.')
    else:
        print('RESULT: Candidate did NOT improve both metrics. Keep original as baseline; refine features.')
    return weights, raw, adjusted


def weekly_rankings(season, train_season, stats, train_stats, schedule, requested_week=None):
    print(f'\nTRAINING ADJUSTMENT USING {train_season} HISTORICAL OUTCOMES ONLY...')
    training = history_rows(train_stats, schedule, train_season)
    weights = fit_logistic(training)
    week, games = weekly.upcoming_games(schedule, season, week=requested_week)
    if not games:
        raise RuntimeError('No future regular-season games found. Try --season or --week.')
    print(f'\nUPCOMING SEASON {season}, WEEK {week} ({len(games)} games remaining)')
    print('Use raw and adjusted percentages as candidate forecasts, NOT validated guarantees.')
    opponents = {}
    game_info = {}
    for g in games:
        opponents[g['home']] = g['away']
        opponents[g['away']] = g['home']
        game_info[g['home']] = g
        game_info[g['away']] = g
    history = team_player_history(stats)
    ranked = pregame_players(stats, week, opponents, history)
    for r in ranked:
        r['adjusted_probability'] = calibrated_probability(r, weights)
        info = game_info[r['team']]
        r['game'] = f"{info['away']} @ {info['home']}"
        r['gameday'] = info['day']
        r['gametime_eastern'] = info['time_eastern']
        r['season'] = season
    ranked.sort(key=lambda x: (x['gameday'], x['gametime_eastern'], x['game'],
                               -x['adjusted_probability']))
    print(f'\nGAME-BY-GAME TD FORECASTS — week {week}\n')
    bygame = defaultdict(list)
    for r in ranked:
        bygame[r['game']].append(r)
    for g in games:
        title = f"{g['away']} @ {g['home']}"
        print(f"{g['day']} {g['time_eastern']} ET  {title}")
        cohort = sorted(bygame.get(title, []), key=lambda x: x['adjusted_probability'], reverse=True)
        if not cohort:
            print('  No modeled players')
        for r in cohort[:5]:
            print(f"  {r['player'][:23]:23s} {r['team']:3s} {r['position']:2s}"
                  f"  Original {r['raw_probability']:6.1%}  Adjusted {r['adjusted_probability']:6.1%}"
                  f"  Missed team games {r['model_metadata']['missed_team_games']}")
        print()
    flat = []
    for r in ranked:
        flat.append({**{k: r[k] for k in ('season','week','gameday','gametime_eastern','game',
                                         'player','team','opponent','position',
                                         'raw_probability','adjusted_probability')},
                     **r['model_metadata']})
    write_csv(f'phase5_week_{week}_{season}.csv', flat,
              ['season','week','gameday','gametime_eastern','game','player','team',
               'opponent','position','raw_probability','adjusted_probability',
               'missed_team_games','recorded_in_recent_3','avg_recent_touches'])
    print('Not included: confirmed player availability, opponent strength, goal-line work, weather.')


def self_test():
    """Offline tests on fictional fixtures: math, no future leakage, output formats."""
    stats = model.nfl_stats(2026, HERE / 'demo_stats.csv')
    history = team_player_history(stats)
    opponents = {'CLE':'PIT','PIT':'CLE'}
    before = pregame_players(stats, 4, opponents, history)
    # Mutating a future game must NOT alter week-4 features or raw model values.
    extra = dict(stats[-1]); extra.update(week='6', carries='10000', targets='10000',
                                       rushing_tds='100', receiving_tds='100')
    after = pregame_players(stats+[extra], 4, opponents)
    assert [(p['team'], p['player'], p['raw_probability'], p['features']) for p in before] == [
           (p['team'], p['player'], p['raw_probability'], p['features']) for p in after], 'LOOKAHEAD ERROR'
    for r in before:
        assert 0 < r['raw_probability'] < 1
        assert len(r['features']) == len(FEATURES)
    assert abs(sigmoid(0) - 0.5) < 1e-12
    assert all(math.isfinite(x) for x in solve([[2,0],[0,3]], [6,12]))
    fake = []
    for i in range(360):
        p = .05 + (i % 16)*.045
        f = (1.0, math.log(p/(1-p)), float(i%4==0), float(i%4==1),
             float(i%4==2), float(i%4==3), float(i%3==0), .8, .5, .8)
        fake.append({'features':f, 'scored': int((i*17)%43 < 43*p)})
    fitted = fit_logistic(fake)
    assert all(math.isfinite(x) for x in fitted)
    assert all(0 <= calibrated_probability(r, fitted) <= 1 for r in fake)
    print(f'SELF-TEST PASS: future-week leakage, {len(before)} fixture forecasts, logistic fitting, math.')


def main():
    ap = argparse.ArgumentParser(description='TD Lab Phase 5: truly held-out calibration experiment')
    options = ap.add_mutually_exclusive_group(required=True)
    options.add_argument('--evaluate', action='store_true', help='Train on older season and evaluate on a newer season')
    options.add_argument('--weekly', action='store_true', help='Show next slate using a past-season adjustment')
    options.add_argument('--self-test', action='store_true', help='Offline fictional tests')
    ap.add_argument('--train-season', type=int, default=2024)
    ap.add_argument('--test-season', type=int, default=2025)
    ap.add_argument('--season', type=int, default=2026, help='Weekly projection season')
    ap.add_argument('--week', type=int, help='Optional future week for --weekly')
    ap.add_argument('--schedule-csv', help='Use local NFL schedule file')
    ap.add_argument('--train-stats-csv', help='Use local training-season nflverse CSV')
    ap.add_argument('--test-stats-csv', help='Use local test-season nflverse CSV')
    ap.add_argument('--stats-csv', help='Use local weekly-season nflverse CSV')
    args = ap.parse_args()
    if args.self_test:
        self_test()
        return
    if args.train_season >= (args.test_season if args.evaluate else args.season):
        raise RuntimeError('Train season must predate the evaluation or live season.')
    schedule = weekly.load_schedule(args.schedule_csv)
    print(f'Downloading/reading {args.train_season} training-season player data...')
    train_stats = model.nfl_stats(args.train_season, args.train_stats_csv)
    if args.evaluate:
        print(f'Downloading/reading {args.test_season} held-out player data...')
        test_stats = model.nfl_stats(args.test_season, args.test_stats_csv)
        evaluate(args.train_season, args.test_season, schedule, train_stats, test_stats)
    else:
        print(f'Downloading/reading {args.season} current-season player data...')
        current_stats = model.nfl_stats(args.season, args.stats_csv)
        weekly_rankings(args.season, args.train_season, current_stats,
                        train_stats, schedule, args.week)


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, ValueError, csv.Error) as error:
        print(f'\nERROR: {error}')
        sys.exit(1)
