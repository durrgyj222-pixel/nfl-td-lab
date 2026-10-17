"""NFL TD Lab Phase 6: test opponent rushing/receiving TD allowances.

Place alongside td_model.py, td_weekly.py, td_backtest.py, td_phase5.py.
Requires only Python's standard library + certifi for project's existing download code.

  py td_phase6.py --self-test
  py td_phase6.py --evaluate --train-season 2024 --test-season 2025
  py td_phase6.py --weekly --season 2026 --train-season 2024

Defense opponent totals are computed ONLY from games before the target week.
Each defense rate is shrunk toward past-to-date league averages (4 prior games).
This tests whether defenses contribute predictive information beyond Phase 5.
No odds, verified active rosters, play-by-play goal line data, or weather yet.
"""
import argparse
import csv
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone

import td_model as m
import td_backtest as bt
import td_weekly as weekly
import td_phase5 as p5

NEW_FEATURES = ('rush_defense_context', 'receive_defense_context')


def game_opponents(schedule, season):
    matchups = {}
    for r in schedule:
        if bt.row_week(r) <= 0:
            continue
        try:
            yr = int(float(r.get('season') or 0))
        except (TypeError, ValueError):
            continue
        if yr != season or (r.get('game_type') or 'REG').upper() != 'REG':
            continue
        w = bt.row_week(r)
        home = m.clean_team(r.get('home_team'))
        away = m.clean_team(r.get('away_team'))
        if home and away:
            matchups[(w, home)] = away
            matchups[(w, away)] = home
    return matchups


def defensive_history(stats, schedule, season):
    """One entry for each defense and actual past game; no results from future.

    Each record: week, rushing TDs allowed, receiving TDs allowed. Limited to
    games where the opponent has an observed team-level player stat row.
    """
    offensive = defaultdict(lambda: [0.0, 0.0])
    exists = set()
    for r in stats:
        if not bt.regular(r):
            continue
        w = bt.row_week(r)
        team = m.clean_team(r.get('team') or r.get('recent_team'))
        if not (w >= 1 and team):
            continue
        exists.add((w, team))
        offensive[(w, team)][0] += m.num(r, 'rushing_tds')
        offensive[(w, team)][1] += m.num(r, 'receiving_tds')
    opps = game_opponents(schedule, season)
    allowed = defaultdict(list)
    for (w, defense), offense in opps.items():
        if (w, offense) in exists:
            rush, receive = offensive[(w, offense)]
            allowed[defense].append((w, rush, receive))
    for games in allowed.values():
        games.sort(key=lambda g: g[0])
    return allowed


def defense_features(allowed, defense, target_week):
    """Relative allowed-TD rates using only earlier weeks, shrunk with 4 games.

    A league baseline is estimated from observed allowed-TD rates in strictly
    preceding weeks. This is useful matchup context, not a causal measure.
    """
    league_rush = league_receive = league_games = 0
    for games in allowed.values():
        for w, r, a in games:
            if w < target_week:
                league_rush += r
                league_receive += a
                league_games += 1
    if league_games == 0:
        return 0., 0., 0, 0., 0.
    # Floors avoid unstable log ratios during low-scoring early weeks.
    avg_r = max(league_rush / league_games, .15)
    avg_a = max(league_receive / league_games, .15)
    own = [(r, a) for w, r, a in allowed.get(defense, ()) if w < target_week]
    n = len(own)
    pseudo = 4
    own_r = (sum(x[0] for x in own) + pseudo * avg_r) / (n + pseudo)
    own_a = (sum(x[1] for x in own) + pseudo * avg_a) / (n + pseudo)
    rush_ratio = p5.clip(own_r / avg_r, .5, 1.6)
    receive_ratio = p5.clip(own_a / avg_a, .5, 1.6)
    return math.log(rush_ratio), math.log(receive_ratio), n, own_r, own_a


def extend_rows(rows, defense_index):
    extended = []
    for r in rows:
        dr, da, n, allowed_r, allowed_a = defense_features(
            defense_index, r['opponent'], r['week'])
        # Each type applies most strongly to the relevant TD path. RBs can
        # also score receiving TDs; the original player score captures usage.
        pos = r['position'].upper()
        rush_context = dr if pos in ('RB', 'QB') else 0.
        receive_context = da if pos in ('RB', 'WR', 'TE') else 0.
        info = dict(r)
        info['features'] = tuple(r['features']) + (rush_context, receive_context)
        info['defense_games'] = n
        info['opponent_rush_td_allowed_pg'] = allowed_r
        info['opponent_receive_td_allowed_pg'] = allowed_a
        info['opponent_rush_relative'] = round(math.exp(dr), 3)
        info['opponent_receive_relative'] = round(math.exp(da), 3)
        extended.append(info)
    return extended


def fit_defense_model(train):
    old = p5.FEATURES
    try:
        p5.FEATURES = old + NEW_FEATURES
        return p5.fit_logistic(train)
    finally:
        p5.FEATURES = old


def add_probs(rows, old_weights, new_weights):
    for r in rows:
        r['phase5_probability'] = p5.sigmoid(sum(a*b for a, b in zip(r['features'][:len(p5.FEATURES)], old_weights)))
        r['phase6_probability'] = p5.sigmoid(sum(a*b for a, b in zip(r['features'], new_weights)))
    return rows


def historical(train_year, test_year, schedule, train_stats, test_stats):
    if train_year >= test_year:
        raise RuntimeError('Training season must precede testing season.')
    print(f'\nBuilding {train_year} training forecasts (past-game features only)...')
    train = p5.history_rows(train_stats, schedule, train_year)
    print(f'\nBuilding {test_year} test forecasts (past-game features only)...')
    test = p5.history_rows(test_stats, schedule, test_year)
    if not train or not test:
        raise RuntimeError('No training or test forecasts; check game schedule/data.')
    train_d = extend_rows(train, defensive_history(train_stats, schedule, train_year))
    test_d = extend_rows(test, defensive_history(test_stats, schedule, test_year))
    original_weights = p5.fit_logistic(train)
    candidate_weights = fit_defense_model(train_d)
    add_probs(test_d, original_weights, candidate_weights)
    return train, test_d, original_weights, candidate_weights


def evaluate(args, schedule, train_stats, test_stats):
    train, test, old_weights, new_weights = historical(
        args.train_season, args.test_season, schedule, train_stats, test_stats)
    print(f'\nPHASE 6 OUT-OF-SAMPLE: trained {args.train_season}, graded {args.test_season}')
    print(f'  Training forecasts {len(train):,} | test forecasts {len(test):,}')
    print(f"  {'Model':<25}{'Brier':>11}{'Log loss':>12}{'Avg P':>11}{'Actual':>11}")
    print('  ' + '-'*70)
    metrics = {}
    for k, label in (('raw_probability', 'Original model'),
                     ('phase5_probability', 'Phase 5 calibrated'),
                     ('phase6_probability', 'Phase 6 + defense')):
        x = p5.metrics(test, k)
        metrics[k] = x
        print(f"  {label:<25}{x['brier']:>11.5f}{x['log_loss']:>12.5f}"
              f"{x['mean_prediction']:>11.1%}{x['actual']:>11.1%}")
    a = metrics['phase5_probability']; b = metrics['phase6_probability']
    print(f'\n  Improvement vs Phase 5: Brier {a["brier"] - b["brier"]:+.5f}; '
          f'Log loss {a["log_loss"] - b["log_loss"]:+.5f}')
    high = [r for r in test if r['phase5_probability'] >= .4]
    if high:
        print(f'  HIGH-PROBABILITY COHORT (Phase 5 >=40%, n={len(high)}):')
        for key, title in (('phase5_probability', 'Phase 5'), ('phase6_probability', 'Phase 6')):
            met = p5.metrics(high, key)
            print(f'    {title}: avg {met["mean_prediction"]:.1%}; scored {met["actual"]:.1%}; '
                  f'Brier {met["brier"]:.4f}')
    fields = ('week','player','team','opponent','position','raw_probability',
              'phase5_probability','phase6_probability','scored','had_same_week_row',
              'defense_games','opponent_rush_td_allowed_pg','opponent_receive_td_allowed_pg',
              'opponent_rush_relative','opponent_receive_relative')
    p5.write_csv(f'phase6_{args.test_season}_validation.csv', test, fields)
    p5.write_csv(f'phase6_{args.train_season}_weights.csv',
                 [{'feature': name, 'coefficient': round(x, 8)}
                  for name, x in zip(p5.FEATURES + NEW_FEATURES, new_weights)],
                 ('feature', 'coefficient'))
    if b['brier'] < a['brier'] and b['log_loss'] < a['log_loss']:
        print('  RESULT: Defense features improved both metrics in this test.')
    else:
        print('  RESULT: Defense features did NOT improve both metrics. DO NOT assume they help.')
    print('  NOTE: 2025 has now been examined repeatedly. A further untouched period is needed.')
    print('  No verified injuries, red-zone usage, betting odds or weather included.')


def weekly_report(args, schedule, train_stats, current_stats):
    print(f'Building historical training forecasts from {args.train_season}...')
    train = p5.history_rows(train_stats, schedule, args.train_season)
    train_d = extend_rows(train, defensive_history(train_stats, schedule, args.train_season))
    old_weights = p5.fit_logistic(train)
    new_weights = fit_defense_model(train_d)
    week, games = weekly.upcoming_games(schedule, args.season, week=args.week)
    if not games:
        raise RuntimeError('No future games found. Check the season or week.')
    opp = {}
    for g in games:
        opp[g['home']] = g['away']; opp[g['away']] = g['home']
    base = p5.pregame_players(current_stats, week, opp)
    rows = extend_rows(base, defensive_history(current_stats, schedule, args.season))
    add_probs(rows, old_weights, new_weights)
    by_team = defaultdict(list)
    for r in rows:
        by_team[r['team']].append(r)
    print(f'\nUPCOMING SEASON {args.season}, WEEK {week}: {len(games)} games left')
    print('Game-by-game TD forecasts: experimental, defense adjustment NOT established as superior.\n')
    exported = []
    for game in games:
        title = f"{game['away']} @ {game['home']}"
        players = sorted(by_team[game['away']] + by_team[game['home']],
                         key=lambda r: -r['phase6_probability'])
        print(f"{game['day']} {game['time_eastern']} ET | {title}")
        for r in players[:5]:
            print(f"  {r['player'][:23]:23s} {r['team']:3s} {r['position']:2s}  "
                  f"Phase5 {r['phase5_probability']:5.1%}  Phase6 {r['phase6_probability']:5.1%}"
                  f"  ({r['defense_games']} opp. past games)")
        print()
        for r in players:
            exported.append({'season': args.season, 'week': week, 'game': title,
                             'gameday':game['day'], 'gametime_eastern':game['time_eastern'], **r})
    p5.write_csv(f'phase6_week_{week}_{args.season}.csv', exported,
                 ('season','week','game','gameday','gametime_eastern', 'player','team',
                  'opponent','position','raw_probability','phase5_probability','phase6_probability',
                  'defense_games','opponent_rush_td_allowed_pg','opponent_receive_td_allowed_pg',
                  'opponent_rush_relative','opponent_receive_relative'))
    print('This is a model experiment, not a verified roster or game weather report.')


def self_test():
    schedule = []
    stats = []
    for week in range(1, 7):
        schedule.append({'season':'2024','week':week,'game_type':'REG',
                         'home_team':'CLE','away_team':'PIT'})
        stats.append({'week':week,'season_type':'REG', 'team':'CLE',
                      'player_display_name':'Fictional A','position':'RB',
                      'rushing_tds': 1 if week % 2 else 0, 'receiving_tds':0,
                      'carries':12, 'targets':0})
        stats.append({'week':week,'season_type':'REG', 'team':'PIT',
                      'player_display_name':'Fictional B','position':'WR',
                      'rushing_tds':0,'receiving_tds':1 if week%2==0 else 0,
                      'carries':0, 'targets':8})
    d = defensive_history(stats, schedule, 2024)
    assert len(d['CLE']) == 6 and len(d['PIT']) == 6
    pre = defense_features(d, 'CLE', 4)
    contaminated = [dict(r) for r in stats]
    for r in contaminated:
        if r['week'] > 3 and r['team'] == 'PIT':
            r['receiving_tds'] = 9000
    post = defense_features(defensive_history(contaminated, schedule, 2024), 'CLE', 4)
    assert pre == post, 'FUTURE DATA LEAK: future-week offense changed prior defense features'
    unknown = defense_features(d, 'XYZ', 4)
    assert abs(unknown[0]) < 1e-10 and abs(unknown[1]) < 1e-10
    testrows = [{'opponent':'CLE','week':4,'position':'RB','features':tuple(range(len(p5.FEATURES))),
                 'scored':0,'raw_probability':.25}]
    extended = extend_rows(testrows, d)
    assert len(extended[0]['features']) == len(p5.FEATURES) + 2
    assert extended[0]['defense_games'] == 3
    print('SELF-TEST PASS: past-only defense rates, no future leakage, feature lengths.')


def main():
    ap = argparse.ArgumentParser(description='NFL TD Lab Phase 6: defensive matchup feature experiment')
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument('--evaluate', action='store_true')
    mode.add_argument('--weekly', action='store_true')
    mode.add_argument('--self-test', action='store_true')
    ap.add_argument('--train-season', type=int, default=2024)
    ap.add_argument('--test-season', type=int, default=2025)
    ap.add_argument('--season', type=int, default=2026)
    ap.add_argument('--week', type=int)
    ap.add_argument('--schedule-csv')
    ap.add_argument('--train-stats-csv')
    ap.add_argument('--test-stats-csv')
    ap.add_argument('--stats-csv')
    args = ap.parse_args()
    if args.self_test:
        self_test()
        return
    if args.evaluate and args.train_season >= args.test_season:
        raise RuntimeError('Training season must be earlier than testing season.')
    schedule = weekly.load_schedule(args.schedule_csv)
    train_stats = m.nfl_stats(args.train_season, args.train_stats_csv)
    if args.evaluate:
        test_stats = m.nfl_stats(args.test_season, args.test_stats_csv)
        evaluate(args, schedule, train_stats, test_stats)
    else:
        current_stats = m.nfl_stats(args.season, args.stats_csv)
        weekly_report(args, schedule, train_stats, current_stats)


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, csv.Error, ValueError) as e:
        print(f'\nERROR: {e}')
        sys.exit(1)
