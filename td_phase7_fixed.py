"""NFL TD Lab Phase 7: FIXED PBP schema compatibility for goal-line data.

Uses nflverse PBP releases (large CSV downloads; cached after first use).
Windows commands:
    py td_phase7_fixed.py --self-test
    py td_phase7_fixed.py --evaluate --train-season 2024 --test-season 2025
    py td_phase7_fixed.py --weekly --season 2026 --train-season 2024

Requires existing td_model.py, td_weekly.py, td_backtest.py, td_phase5.py,
    td_phase6.py and certifi in same folder / Python installation.

No wagering, price feeds, verified active rosters, or weather data used.
The 2025 season has been reused; treat all 2025 results as exploratory.
"""
import argparse
import csv
import io
import math
import os
import ssl
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import td_model as m
import td_weekly as weekly
import td_backtest as bt
import td_phase5 as p5
import td_phase6 as p6

PBP_URL = ('https://github.com/nflverse/nflverse-data/releases/download/'
           'pbp/play_by_play_{season}.csv')
CACHE_DIR = Path(__file__).resolve().parent / 'cache'
REDZONE_FEATURES = ('inside5_rush_per_recent_game',
                    'inside10_other_rush_per_recent_game',
                    'inside20_targets_per_recent_game')
# We avoid mixing receiver names from PBP with weekly stat names; IDs only.


def get_pbp_path(season, supplied=None):
    if supplied:
        path = Path(supplied).expanduser()
        if not path.is_file():
            raise RuntimeError(f'Play-by-play CSV does not exist: {path}')
        return path
    CACHE_DIR.mkdir(exist_ok=True)
    path = CACHE_DIR / f'play_by_play_{season}.csv'
    # During active season data changes; historical full-season data is treated as static.
    age = 12*3600 if season >= 2026 else 30*24*3600
    if path.exists() and path.stat().st_size > 1000 and time.time()-path.stat().st_mtime < age:
        print(f'Using cached {path.name} ({path.stat().st_size / 1048576:.1f} MB)')
        return path
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
    url = PBP_URL.format(season=season)
    print(f'Downloading {season} nflverse PLAY-BY-PLAY (may take several minutes, large CSV)...')
    req = Request(url, headers={'User-Agent': 'NFL-TD-Lab/phase7 research'})
    temp_path = None
    try:
        with urlopen(req, context=ctx, timeout=90) as response:
            with tempfile.NamedTemporaryFile(dir=CACHE_DIR, prefix=f'pbp_{season}_',
                                             suffix='.tmp', delete=False) as out:
                temp_path = Path(out.name)
                while True:
                    part = response.read(1024*1024)
                    if not part:
                        break
                    out.write(part)
        if temp_path.stat().st_size < 1000:
            raise RuntimeError(f'{season} play-by-play download is unexpectedly small.')
        os.replace(temp_path, path)
    except (OSError, HTTPError, URLError, TimeoutError) as exc:
        if temp_path and temp_path.exists():
            temp_path.unlink()
        if path.exists() and path.stat().st_size > 1000:
            print(f'WARNING: download failed; using cached {path.name}: {exc}')
            return path
        raise RuntimeError(f'Could not download {season} play-by-play from nflverse: {exc}') from exc
    return path


def flag(row, field):
    try:
        return float(row.get(field) or 0) == 1.0
    except (ValueError, TypeError):
        return False


def by_id_week_stats(stats):
    """Resolve (week, team, normalized name) => GSIS ID from week-level stats.
    Only rows from weeks preceding forecast are ever used for feature values.
    """
    named = defaultdict(list)
    for r in stats:
        if not bt.regular(r):
            continue
        w = bt.row_week(r)
        team = m.clean_team(r.get('team') or r.get('recent_team'))
        name = m.clean_name(r.get('player_display_name') or r.get('player_name'))
        pid = str(r.get('player_id') or '').strip()
        if w > 0 and team and name and pid and pid.upper() not in ('NA','NAN','NONE'):
            named[(team,name)].append((w,pid))
    for key in named:
        named[key].sort(reverse=True)
    return named


def prior_player_id(lookup, team, name, target_week):
    for w, pid in lookup.get((team,m.clean_name(name)), ()):
        if w < target_week:
            return pid
    return None


def read_redzone_plays(path, season):
    """Store aggregated per-player per-week goal-line and red-zone opportunities.

    Only REG plays not invalidated by a penalty; rushing attempts in the
    opponent's <=10, and targeted passes inside the opponent's 20.
    Supports nflverse play_type=no_play when there is no no_play flag.
    Data is streamed one CSV row at a time to avoid high memory usage.
    """
    required = {'week','season_type','posteam','yardline_100','rush_attempt',
                'pass_attempt','rusher_player_id','receiver_player_id'}
    measures = defaultdict(lambda: [0, 0, 0])
    totals = {'plays_read': 0, 'plays_eligible': 0, 'inside5': 0, 'inside10': 0,
              'inside20_targets': 0}
    try:
        with Path(path).open('r', encoding='utf-8-sig', newline='') as file:
            reader = csv.DictReader(file)
            found = set(reader.fieldnames or ())
            if required - found:
                raise RuntimeError(f'PBP CSV missing fields: {", ".join(sorted(required - found))}. '
                                   'Use nflverse play-by-play CSV, not the weekly-stats CSV.')
            if 'no_play' not in found and 'play_type' not in found:
                raise RuntimeError('PBP CSV needs either no_play or play_type to identify voided plays.')
            if 'no_play' not in found:
                print('  Schema note: no_play column absent; using play_type to exclude voided plays.')
            if 'season' in found:
                check_season = True
            else:
                check_season = False
            for r in reader:
                totals['plays_read'] += 1
                if (r.get('season_type') or '').upper() not in ('REG', 'REGULAR'):
                    continue
                if check_season and str(r.get('season') or '').strip() != str(season):
                    continue
                # nflverse uses play_type='no_play' for penalty-negated plays.
                # Many current PBP releases do not supply a separate no_play column.
                if flag(r, 'no_play'):
                    continue
                if 'play_type' in found:
                    # Count only actual rush/pass plays, not penalties, notes,
                    # timeouts, kicks, spikes, or voided ('no_play') events.
                    if (r.get('play_type') or '').strip().lower() not in ('run', 'pass'):
                        continue
                w = bt.row_week(r)
                team = m.clean_team(r.get('posteam'))
                if w <= 0 or not team:
                    continue
                try:
                    yards_to_goal = float(r.get('yardline_100') or 'nan')
                except ValueError:
                    continue
                if not math.isfinite(yards_to_goal) or not 0 < yards_to_goal <= 20:
                    continue
                totals['plays_eligible'] += 1
                if flag(r, 'rush_attempt'):
                    pid = str(r.get('rusher_player_id') or '').strip()
                    if pid and pid.upper() not in ('NA','NAN','NONE') and yards_to_goal <= 10:
                        key = (w, team, pid)
                        if yards_to_goal <= 5:
                            measures[key][0] += 1
                            totals['inside5'] += 1
                        else:
                            measures[key][1] += 1
                            totals['inside10'] += 1
                elif flag(r, 'pass_attempt'):
                    pid = str(r.get('receiver_player_id') or '').strip()
                    if pid and pid.upper() not in ('NA','NAN','NONE'):
                        measures[(w, team, pid)][2] += 1
                        totals['inside20_targets'] += 1
    except UnicodeDecodeError as exc:
        raise RuntimeError('The supplied PBP file must be a UTF-8 CSV.') from exc
    if totals['plays_read'] == 0:
        raise RuntimeError('Play-by-play CSV is empty.')
    print('  PBP read: {plays_read:,} plays; inside-5 carries {inside5:,}, '
          'other inside-10 carries {inside10:,}, red-zone targets {inside20_targets:,}'
          .format(**totals))
    if sum(totals[k] for k in ('inside5','inside10','inside20_targets')) == 0:
        raise RuntimeError('No usable red-zone events in PBP CSV; check data and column definitions.')
    return measures, totals


def feature_redzone(row, id_index, pbp_index, all_team_games):
    """Features use only games in same season BEFORE r['week'].
    Counting per TEAM game, not player appearance, minimizes inactive-player inflation.
    """
    week = row['week']
    team = row['team']
    pid = prior_player_id(id_index, team, row['player'], week)
    last = sorted(w for w in all_team_games.get(team, ()) if w < week)[-4:]
    counts = [0, 0, 0]
    if pid:
        for w in last:
            vec = pbp_index.get((w, team, pid))
            if vec:
                counts = [a+b for a,b in zip(counts, vec)]
    denom = max(1, len(last))
    # Regressor transforms handle high counts without dominating logit scales.
    features = tuple(math.log1p(x/denom) for x in counts)
    metadata = {'gsis_id_match': int(bool(pid)), 'recent_team_games': len(last),
                'inside5_rush_last4': counts[0], 'inside6to10_rush_last4': counts[1],
                'rz_targets_last4': counts[2]}
    return features, metadata


def extend_rows(rows, stats, measures):
    lookups = by_id_week_stats(stats)
    hist = p5.team_player_history(stats)
    team_games = hist[0]
    extended = []
    for row in rows:
        ext = dict(row)
        feats, meta = feature_redzone(row, lookups, measures, team_games)
        ext['features'] = tuple(row['features']) + feats
        ext.update(meta)
        extended.append(ext)
    return extended


def fit_phase7(rows):
    old = p5.FEATURES
    try:
        p5.FEATURES = old + p6.NEW_FEATURES + REDZONE_FEATURES
        return p5.fit_logistic(rows)
    finally:
        p5.FEATURES = old


def fit_phase6(rows):
    return p6.fit_defense_model(rows)


def train_pair(train6, train7):
    return fit_phase6(train6), fit_phase7(train7)


def assign_probabilities(rows, w6, w7):
    n = len(p5.FEATURES) + len(p6.NEW_FEATURES)
    for r in rows:
        r['phase6_probability'] = p5.sigmoid(sum(x*w for x,w in zip(r['features'][:n], w6)))
        r['phase7_probability'] = p5.sigmoid(sum(x*w for x,w in zip(r['features'], w7)))
    return rows


def build_train_test(schedule, train_year, test_year, train_stats, test_stats, train_pbp, test_pbp):
    if train_year >= test_year:
        raise RuntimeError('Training year must precede testing year.')
    print(f'\nBuilding past-only {train_year} training data...')
    base_train = p5.history_rows(train_stats, schedule, train_year)
    print(f'\nBuilding past-only {test_year} evaluation data...')
    base_test = p5.history_rows(test_stats, schedule, test_year)
    if not base_train or not base_test:
        raise RuntimeError('No forecast rows were generated.')
    dtrain = p6.extend_rows(base_train, p6.defensive_history(train_stats, schedule, train_year))
    dtest = p6.extend_rows(base_test, p6.defensive_history(test_stats, schedule, test_year))
    t7 = extend_rows(dtrain, train_stats, train_pbp)
    e7 = extend_rows(dtest, test_stats, test_pbp)
    tmatched = sum(r['gsis_id_match'] for r in t7)
    ematched = sum(r['gsis_id_match'] for r in e7)
    print(f'Player IDs matched (using PREVIOUS weekly stat rows): '
          f'train {tmatched:,}/{len(t7):,}; test {ematched:,}/{len(e7):,}')
    if min(tmatched/len(t7), ematched/len(e7)) < .60:
        raise RuntimeError('Too few GSIS player IDs matched; refusing to evaluate potentially invalid joins.')
    return dtrain, t7, e7


def print_metric(label, met):
    print(f"  {label:<29}{met['brier']:>11.5f}{met['log_loss']:>12.5f}"
          f"{met['mean_prediction']:>11.1%}{met['actual']:>11.1%}")


def evaluate(args, schedule, train_stats, test_stats, train_pbp, test_pbp):
    base6, train7, test7 = build_train_test(schedule,args.train_season,args.test_season,
                                          train_stats,test_stats,train_pbp,test_pbp)
    w6, w7 = train_pair(base6, train7)
    assign_probabilities(test7, w6, w7)
    met6 = p5.metrics(test7,'phase6_probability')
    met7 = p5.metrics(test7,'phase7_probability')
    print(f'\nPHASE 7 TEST: trained {args.train_season}, evaluated {args.test_season}')
    print(f'  Forecasts {len(test7):,}; no future game statistics used as model inputs')
    print(f"  {'Model':<29}{'Brier':>11}{'Log loss':>12}{'Avg P':>11}{'Actual':>11}")
    print('  ' + '-'*74)
    print_metric('Phase 6 (defense)',met6)
    print_metric('Phase 7 (+red zone)',met7)
    print(f"\n  Difference (Phase 6 minus 7): Brier {met6['brier']-met7['brier']:+.5f}, "
          f"log loss {met6['log_loss']-met7['log_loss']:+.5f}")
    high = [r for r in test7 if r['phase6_probability'] >= .4]
    if high:
        print(f'  High-probability group (phase 6 >=40%, n={len(high)}):')
        print_metric('Phase 6',p5.metrics(high,'phase6_probability'))
        print_metric('Phase 7',p5.metrics(high,'phase7_probability'))
    for r in test7:
        r['season'] = args.test_season
    p5.write_csv(f'phase7_{args.test_season}_validation.csv', test7,
                 ('season','week','player','team','opponent','position',
                  'phase6_probability','phase7_probability','scored','had_same_week_row',
                  'gsis_id_match','recent_team_games','inside5_rush_last4',
                  'inside6to10_rush_last4','rz_targets_last4'))
    p5.write_csv(f'phase7_{args.train_season}_weights.csv',
                 [{'feature': k,'coefficient': round(w,8)} for k,w in zip(
                     p5.FEATURES + p6.NEW_FEATURES + REDZONE_FEATURES,w7)],
                 ('feature','coefficient'))
    if met7['brier'] < met6['brier'] and met7['log_loss'] < met6['log_loss']:
        print('EXPERIMENT: phase 7 improved both metrics on this period.')
    else:
        print('EXPERIMENT: phase 7 did not improve both metrics; do NOT promote it.')
    print('CAUTION: 2025 has been repeatedly inspected, so not a fresh holdout; '
          'further validation is required.')
    print('No game-day lineup confirmation, weather or market prices included.')


def weekly_report(args, schedule, train_stats, current_stats, train_pbp, current_pbp):
    print(f'\nBuilding {args.train_season} historical training forecasts...')
    train = p5.history_rows(train_stats,schedule,args.train_season)
    train6 = p6.extend_rows(train,p6.defensive_history(train_stats,schedule,args.train_season))
    train7 = extend_rows(train6,train_stats,train_pbp)
    if sum(r['gsis_id_match'] for r in train7) / len(train7) < .60:
        raise RuntimeError('Training player IDs could not be matched safely.')
    w6,w7 = train_pair(train6,train7)
    week,games = weekly.upcoming_games(schedule,args.season,week=args.week)
    if not games:
        raise RuntimeError('No future games remaining for the requested NFL week.')
    opp={}
    for g in games:
        opp[g['away']]=g['home']; opp[g['home']]=g['away']
    pregames = p5.pregame_players(current_stats,week,opp)
    six = p6.extend_rows(pregames,p6.defensive_history(current_stats,schedule,args.season))
    seven = extend_rows(six,current_stats,current_pbp)
    assign_probabilities(seven,w6,w7)
    by_team=defaultdict(list)
    for r in seven:
        by_team[r['team']].append(r)
    output=[]
    print(f'\nUPCOMING WEEK {week} ({args.season}), {len(games)} games left; '
          'EXPERIMENTAL TOUCHDOWN PROJECTIONS')
    for g in games:
        title=f"{g['away']} @ {g['home']}"
        players=sorted(by_team[g['away']]+by_team[g['home']],
                       key=lambda x:-x['phase7_probability'])
        print(f"\n  {g['day']} {g['time_eastern']} ET  {title}")
        for r in players[:5]:
            print(f"  {r['player'][:22]:22s} {r['team']:3s} {r['position']:2s}  "
                  f"P6 {r['phase6_probability']:5.1%} -> P7 {r['phase7_probability']:5.1%} "
                  f"| inside5 {r['inside5_rush_last4']}, inside10-extra "
                  f"{r['inside6to10_rush_last4']}, RZ targets {r['rz_targets_last4']}")
        for r in players:
            output.append(dict(r,season=args.season,game=title,gameday=g['day'],
                               gametime_eastern=g['time_eastern']))
    p5.write_csv(f'phase7_week_{week}_{args.season}.csv',output,
                 ('season','week','game','gameday','gametime_eastern','player','team','opponent',
                  'position','phase6_probability','phase7_probability','gsis_id_match',
                  'inside5_rush_last4','inside6to10_rush_last4','rz_targets_last4'))
    print('WARNING: red-zone features have not proven beneficial on a fresh future season.')


def self_test():
    # Fixture with one player and a competing teammate; dates deliberately varied.
    s=[{'week':str(w),'season_type':'REG','team':'CLE','player_display_name':'Ada Alpha',
        'player_id':'00-00001','position':'RB','carries':'10','targets':'2'}
       for w in range(1,6)]
    row={'week':4,'team':'CLE','player':'Ada Alpha','position':'RB','features':(1.,)}
    # Team missed a week, test normalized by PLAYED games and respect week cutoff.
    look=by_id_week_stats(s)
    assert prior_player_id(look,'CLE','Ada Alpha',4)=='00-00001'
    a={(2,'CLE','00-00001'):[2,1,2],(3,'CLE','00-00001'):[1,0,2],
       (5,'CLE','00-00001'):[900,900,900]}
    games=p5.team_player_history(s)[0]
    f, meta=feature_redzone(row,look,a,games)
    assert meta['inside5_rush_last4']==3 and meta['rz_targets_last4']==4
    assert math.isclose(f[0],math.log1p(3/3))
    row2=dict(row,week=3)
    older,_=feature_redzone(row2,look,a,games)
    a[(5,'CLE','00-00001')]=[12345,12345,12345]
    f2,_=feature_redzone(row,look,a,games)
    older2,_=feature_redzone(row2,look,a,games)
    assert f2==f and older2==older, 'FUTURE LEAK: later redzone plays changed earlier features'
    # End-to-end PBP parser: inside-5 carry, other inside-10, inside-20 target,
    # invalidated no-play ignored, and another season excluded.
    headings=['season','season_type','week','posteam','yardline_100',
              'rush_attempt','pass_attempt','no_play','rusher_player_id','receiver_player_id']
    sample=[['2024','REG','1','CLE','3','1','0','0','00-00001',''],
            ['2024','REG','2','CLE','9','1','0','0','00-00001',''],
            ['2024','REG','3','CLE','18','0','1','0','','00-00001'],
            ['2024','REG','3','CLE','3','1','0','1','00-00001',''],
            ['2025','REG','1','CLE','3','1','0','0','00-00001','']]
    with tempfile.TemporaryDirectory() as d:
        path=Path(d)/'pbp.csv'
        with path.open('w',newline='',encoding='utf-8') as fp:
            writer=csv.writer(fp); writer.writerow(headings); writer.writerows(sample)
        measurements,counts=read_redzone_plays(path,2024)
        assert counts['inside5']==1 and counts['inside10']==1 and counts['inside20_targets']==1
        assert sum(sum(v) for v in measurements.values())==3
        # Real nflverse CSV compatibility: often has play_type, not no_play.
        # A negated rush can have rush_attempt=1 but must not count.
        path2=Path(d)/'pbp_real_schema.csv'
        headings2=[x for x in headings if x!='no_play']+['play_type']
        sample2=[['2024','REG','1','CLE','3','1','0','00-00001','','run'],
                 ['2024','REG','2','CLE','9','1','0','00-00001','','run'],
                 ['2024','REG','3','CLE','18','0','1','','00-00001','pass'],
                 ['2024','REG','3','CLE','3','1','0','00-00001','','no_play'],
                 ['2025','REG','1','CLE','3','1','0','00-00001','','run']]
        with path2.open('w',newline='',encoding='utf-8') as fp:
            writer=csv.writer(fp);writer.writerow(headings2);writer.writerows(sample2)
        measurements2,counts2=read_redzone_plays(path2,2024)
        assert measurements2==measurements, (measurements2, measurements)
        assert counts2['inside5']==1 and counts2['inside10']==1 and counts2['inside20_targets']==1
    print('SELF-TEST PASS: ID matching, streaming PBP, invalidated plays, '
          'future-week isolation and feature math.')


def main():
    parser=argparse.ArgumentParser(description='NFL TD Lab: red-zone feature experiment')
    modes=parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--self-test',action='store_true')
    modes.add_argument('--evaluate',action='store_true')
    modes.add_argument('--weekly',action='store_true')
    parser.add_argument('--train-season',type=int,default=2024)
    parser.add_argument('--test-season',type=int,default=2025)
    parser.add_argument('--season',type=int,default=2026)
    parser.add_argument('--week',type=int)
    parser.add_argument('--schedule-csv')
    parser.add_argument('--train-stats-csv')
    parser.add_argument('--test-stats-csv')
    parser.add_argument('--stats-csv')
    parser.add_argument('--train-pbp-csv')
    parser.add_argument('--test-pbp-csv')
    parser.add_argument('--pbp-csv')
    a=parser.parse_args()
    if a.self_test:
        self_test();return
    if a.evaluate and a.train_season>=a.test_season:
        raise RuntimeError('Train year must be earlier than test year.')
    schedule=weekly.load_schedule(a.schedule_csv)
    training=m.nfl_stats(a.train_season,a.train_stats_csv)
    train_file=get_pbp_path(a.train_season,a.train_pbp_csv)
    train_pbp,_=read_redzone_plays(train_file,a.train_season)
    if a.evaluate:
        testing=m.nfl_stats(a.test_season,a.test_stats_csv)
        test_file=get_pbp_path(a.test_season,a.test_pbp_csv)
        test_pbp,_=read_redzone_plays(test_file,a.test_season)
        evaluate(a,schedule,training,testing,train_pbp,test_pbp)
    else:
        current=m.nfl_stats(a.season,a.stats_csv)
        this_file=get_pbp_path(a.season,a.pbp_csv)
        current_pbp,_=read_redzone_plays(this_file,a.season)
        weekly_report(a,schedule,training,current,train_pbp,current_pbp)


if __name__=='__main__':
    try:
        main()
    except (RuntimeError,OSError,csv.Error,ValueError) as exc:
        print(f'\nERROR: {exc}')
        sys.exit(1)
