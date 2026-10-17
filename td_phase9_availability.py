"""NFL TD Lab | Phase 9: injury-report awareness + app-ready game JSON.

Put this file beside td_model.py, td_weekly.py and Phase 8 script in NFL_TD_Lab.

Commands:
  py td_phase9_availability.py --self-test
  py td_phase9_availability.py --season 2026
  py td_phase9_availability.py --season 2026 --injuries-csv my_injuries.csv
  py td_phase9_availability.py --season 2026 --offline

Reads existing Phase 8 players CSV, uses nflverse injury REPORTS where available,
and optional locally VERIFIED official inactive overrides. DOES NOT estimate
injury-adjusted TD probabilities. Unknown status is never labeled 'active'.
'OUT' and 'INACTIVE' are excluded from the display of top candidates only,
not from exported full player records. Questionable/doubtful stay visible with
prominent status flags. NFL injury reports are not official game-day inactives.

Standard-library only, uses certifi on Windows if installed. No API key.
"""
from __future__ import annotations

import argparse
import csv
import json
import ssl
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import td_model as model
import td_weekly as weekly

HERE = Path(__file__).resolve().parent
OUT = HERE / 'output'
INJURIES_URL = ('https://github.com/nflverse/nflverse-data/releases/download/'
                'injuries/injuries_{season}.csv')
OVERRIDE_COLUMNS = ('season','week','team','player','status','note','verified_at_utc')
UNAVAILABLE = {'OUT','INACTIVE','IR','INJURED RESERVE','PUP','NFI','SUSPENDED'}
UNCERTAIN = {'QUESTIONABLE','DOUBTFUL'}
PREFERRED = ('season','week','game','player','team','opponent','position',
             'phase7_probability','availability','availability_source',
             'reported_game_status','injury','practice_status','report_updated',
             'availability_note','weather_flags','kickoff_utc')


def load_rows(path):
    with Path(path).open('r', encoding='utf-8-sig', newline='') as fp:
        return list(csv.DictReader(fp))


def save_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(dict.fromkeys(PREFERRED + tuple(k for row in rows for k in row)))
    with path.open('w', encoding='utf-8-sig', newline='') as fp:
        writer = csv.DictWriter(fp, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)


def key(team, name):
    return model.clean_team(team), model.clean_name(name)


def text(row, *names):
    return next((str(row.get(k)).strip() for k in names if str(row.get(k) or '').strip()), '')


def norm_status(v):
    raw = ' '.join(str(v or '').strip().replace('_',' ').replace('-', ' ').upper().split())
    aliases = {'Q':'QUESTIONABLE','D':'DOUBTFUL', 'O':'OUT', 'INJURY RESERVE':'IR',
               'INJURED RESERVE':'IR','RESERVE INJURED':'IR',
               'CONFIRMED INACTIVE':'INACTIVE','ACTIVE':'ACTIVE',
               'CONFIRMED ACTIVE':'CONFIRMED ACTIVE',
               'NOT LISTED':'NOT LISTED','': 'UNKNOWN', 'NONE':'UNKNOWN'}
    return aliases.get(raw,raw)


def classify(status, override=False):
    s = norm_status(status)
    if s in UNAVAILABLE:
        return 'EXCLUDED: REPORTED OUT' if not override else 'EXCLUDED: VERIFIED INACTIVE'
    if s in UNCERTAIN:
        return 'UNCERTAIN: ' + s
    if override and s == 'CONFIRMED ACTIVE':
        return 'CONFIRMED ACTIVE (MANUAL)'
    return 'UNKNOWN / NOT VERIFIED'


def fetch_injuries(season, opener=None):
    url = INJURIES_URL.format(season=season)
    if opener is not None:
        data = opener(url)
        if isinstance(data, str):
            data = data.encode('utf-8')
        return data
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
    request = Request(url, headers={'User-Agent':'NFL-TD-Lab-Phase9/1.0 (educational research)'})
    with urlopen(request, timeout=35, context=ctx) as response:
        data = response.read(16 * 1024 * 1024 + 1)
    if len(data) > 16 * 1024 * 1024:
        raise RuntimeError('Injury CSV unexpectedly large (>16MB), refusing to parse.')
    return data


def read_injuries(season, week, path=None, offline=False, opener=None):
    """Return (latest per-player report, provenance, note).

    Injury source is a mutable *current* season file. Never replay it as if it
    were a time-accurate historical snapshot for backtesting.
    """
    if offline and path:
        raise ValueError('Use either --offline or --injuries-csv, not both.')
    if offline:
        return {}, 'UNAVAILABLE (OFFLINE)', 'Injury reports not fetched.'
    try:
        if path:
            rows = load_rows(path)
            provenance = f'LOCAL FILE: {Path(path).name}'
        else:
            data = fetch_injuries(season, opener=opener)
            rows = list(csv.DictReader(data.decode('utf-8-sig').splitlines()))
            provenance = 'NFLVERSE INJURY FEED (CURRENT FILE)'
        if not rows:
            return {}, provenance, 'The injury CSV is empty; player status remains unknown.'
        missing = {'week','team','full_name','report_status'} - set(rows[0])
        if missing:
            raise ValueError('Injury CSV missing required columns: '+', '.join(sorted(missing)))
        latest = {}
        for row in rows:
            try:
                row_week = int(float(row.get('week') or -1))
                row_season = int(float(row.get('season') or season))
            except (ValueError,TypeError):
                continue
            if row_week != week or row_season != season:
                continue
            k = key(row.get('team'), row.get('full_name'))
            if not all(k):
                continue
            modified = text(row, 'date_modified')
            # Keep most recently modified row per player. Lexical order works for
            # consistent ISO date strings; the raw value is exported for audit.
            if k not in latest or modified >= text(latest[k], 'date_modified'):
                latest[k] = row
        if not latest:
            return {}, provenance, f'No season {season} week {week} injury entries; NOT evidence players are healthy.'
        return latest, provenance, f'{len(latest)} player entries found for week {week}.'
    except (OSError,ValueError,UnicodeDecodeError,csv.Error,HTTPError,URLError,TimeoutError) as e:
        return {}, 'UNAVAILABLE (ERROR)', f'Injury feed unavailable: {str(e)[:220]}. Status remains UNKNOWN.'


def create_override_template(path):
    path = Path(path)
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w',encoding='utf-8-sig',newline='') as fp:
        writer=csv.writer(fp);writer.writerow(OVERRIDE_COLUMNS)
    return True


def read_overrides(path,season,week):
    if not Path(path).exists():
        create_override_template(path)
        return {}
    rows=load_rows(path)
    if not rows:
        return {}
    missing={'team','player','status'}-set(rows[0])
    if missing:
        raise ValueError('Override CSV missing: '+', '.join(sorted(missing)))
    matches={}
    for r in rows:
        if not r.get('player') or not r.get('team'):
            continue
        try:
            if 'season' in r and r['season'].strip() and int(r['season']) != season:
                continue
            if 'week' in r and r['week'].strip() and int(r['week']) != week:
                continue
        except ValueError:
            continue
        status=norm_status(r.get('status'))
        if status not in UNAVAILABLE | UNCERTAIN | {'CONFIRMED ACTIVE'}:
            raise ValueError(f'Unsupported manual status for {r["player"]!r}: {r["status"]!r}. '
                             'Use OUT, INACTIVE, IR, QUESTIONABLE, DOUBTFUL, or CONFIRMED ACTIVE.')
        matches[key(r['team'],r['player'])]=r
    return matches


def percent(p):
    try:
        v=float(p)
        return max(0.,min(1.,v))
    except (ValueError,TypeError):
        return 0.


def join_availability(players, injuries, overrides, source):
    annotated=[]
    for orig in players:
        p=dict(orig)
        k=key(p.get('team'),p.get('player'))
        row=injuries.get(k)
        override=overrides.get(k)
        report_status = text(row or {}, 'report_status')
        injury = text(row or {}, 'report_primary_injury')
        practice = text(row or {}, 'practice_status')
        updated = text(row or {}, 'date_modified')
        if override:
            status=norm_status(override.get('status'))
            available=classify(status, override=True)
            src='MANUAL VERIFIED OVERRIDE'
            note=text(override,'note') or 'Manually entered; confirm against official inactives.'
            updated=text(override,'verified_at_utc') or updated
        elif row:
            status=norm_status(report_status)
            available=classify(status)
            src=source
            note='Practice participation is not game status; official actives/inactives not verified.'
        else:
            status='UNKNOWN'
            available='UNKNOWN / NOT VERIFIED'
            src='NO MATCHING WEEKLY INJURY ENTRY'
            note='No report entry does not confirm player active or healthy.'
        p.update({'availability':available,'availability_source':src,
                  'reported_game_status':status,'injury':injury,
                  'practice_status':practice,'report_updated':updated,
                  'availability_note':note,
                  'display_candidate': 'NO' if available.startswith('EXCLUDED:') else 'YES'})
        annotated.append(p)
    return annotated


def build_cards(games, players, generated):
    grouped=defaultdict(list)
    for row in players:
        grouped[row.get('game','')].append(row)
    cards=[]
    for game in games:
        label=f"{game['away']} @ {game['home']}"
        all_players=sorted(grouped.get(label,[]),key=lambda p:-percent(p.get('phase7_probability')))
        eligible=[p for p in all_players if p['display_candidate']=='YES']
        first=all_players[0] if all_players else {}
        def card_player(p):
            return {'name':p.get('player'), 'team':p.get('team'), 'opponent':p.get('opponent'),
                    'position':p.get('position'), 'td_probability':percent(p.get('phase7_probability')),
                    'td_probability_pct':round(100*percent(p.get('phase7_probability')),1),
                    'status':p['availability'], 'reported_game_status':p['reported_game_status'],
                    'injury':p.get('injury'), 'practice_status':p.get('practice_status'),
                    'status_source':p['availability_source'],
                    'red_zone_targets_last4':p.get('rz_targets_last4'),
                    'inside5_carries_last4':p.get('inside5_rush_last4')}
        cards.append({'game':label,'kickoff_utc':game['start'].isoformat(),
            'week':game['week'],'weather':{'stadium':first.get('stadium',''),
                      'roof':first.get('roof',''),
                      'temperature_f':first.get('temperature_f',''),
                      'wind_mph_max':first.get('wind_mph_max',''),
                      'gust_mph_max':first.get('gust_mph_max',''),
                      'precip_probability_pct_max':first.get('precip_probability_pct_max',''),
                      'weather_flags':first.get('weather_flags',''),
                      'forecast_retrieved_at_utc':first.get('retrieved_at_utc','')},
            'favorites':[card_player(p) for p in eligible[:5]],
            'excluded':[card_player(p) for p in all_players if p['display_candidate']=='NO'],
            'all_players':[card_player(p) for p in all_players]})
    return {'app_data_version':1, 'generated_at_utc':generated,
            'note':'Experimental P7 probabilities unchanged; non-excluded does not mean officially active.',
            'games':cards}


def run(season,week=None, injury_csv=None, player_csv=None, schedule_csv=None,
        overrides_csv=None, offline=False, now=None, quiet=False, opener=None):
    now=now or datetime.now(timezone.utc)
    schedule=weekly.load_schedule(schedule_csv)
    selected,games=weekly.upcoming_games(schedule,season,now=now,week=week)
    if not games:
        raise RuntimeError('No upcoming games found for chosen season/week.')
    src=Path(player_csv) if player_csv else OUT/f'phase8_week_{selected}_{season}_players.csv'
    if not src.exists():
        raise RuntimeError(f'Phase 8 player CSV not found: {src}\n'
                           f'Run: py td_phase8_weather.py --season {season} --week {selected}')
    players=load_rows(src)
    required={'player','game','team','phase7_probability'}
    if not players or required-set(players[0]):
        raise RuntimeError('Phase 8 CSV empty or missing required columns: '
                           +', '.join(sorted(required-set(players[0]) if players else required)))
    # Prevent accidentally mixing a past week's predictions into another slate.
    if any(str(row.get('week') or selected)!=str(selected) for row in players):
        raise RuntimeError('Phase 8 CSV contains a different week. Regenerate Phase 8 data.')
    if any(str(row.get('season') or season)!=str(season) for row in players):
        raise RuntimeError('Phase 8 CSV contains a different season.')
    injuries,source,report_note=read_injuries(season,selected,injury_csv,offline,opener=opener)
    overrides_path=Path(overrides_csv) if overrides_csv else HERE/'availability_overrides.csv'
    made=create_override_template(overrides_path)
    overrides=read_overrides(overrides_path,season,selected)
    annotated=join_availability(players,injuries,overrides,source)
    upcoming_labels={f"{g['away']} @ {g['home']}" for g in games}
    annotated=[p for p in annotated if p.get('game') in upcoming_labels]
    generated=datetime.now(timezone.utc).isoformat(timespec='seconds')
    for p in annotated:
        p['generated_at_utc']=generated
    cards=build_cards(games,annotated,generated)
    base=f'phase9_week_{selected}_{season}'
    all_path=OUT/(base+'_players.csv')
    json_path=OUT/(base+'_games.json')
    save_rows(all_path,annotated)
    with json_path.open('w',encoding='utf-8') as fp:
        json.dump(cards,fp,indent=2,ensure_ascii=False)
    # Keep an immutable timestamped snapshot so future results can be audited.
    archive_dir=OUT/'archive'
    archive_dir.mkdir(parents=True,exist_ok=True)
    archive_path=archive_dir/(base+'_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.json')
    with archive_path.open('x',encoding='utf-8') as fp:
        json.dump(cards,fp,indent=2,ensure_ascii=False)
    if not quiet:
        print(f'\nPHASE 9 — SEASON {season} WEEK {selected} | {len(games)} future games')
        print('Player availability context added. Phase 7 probabilities UNCHANGED.')
        print(f'Injury source: {source} — {report_note}')
        print(f'Manual override file: {overrides_path}' + (' (template created)' if made else ''))
        if source.startswith('UNAVAILABLE') or not injuries:
            print('WARNING: absence of a report is UNKNOWN, not confirmation of active status.')
        for game in cards['games']:
            print(f"\n  {game['game']}  |  {game['weather']['weather_flags'] or 'WEATHER NOT RECORDED'}")
            for p in game['favorites']:
                flag=' !' if p['status'].startswith('UNCERTAIN') else ''
                print(f"    {p['name'][:23]:23} {p['td_probability_pct']:5.1f}%  {p['status']}{flag}")
            for p in game['excluded'][:5]:
                print(f"    EXCLUDED {p['name'][:22]:22} {p['reported_game_status']}")
        print(f'\nTotal players: {len(annotated)} | ' +
              ', '.join(f'{v} {k.lower()}' for k,v in Counter(x['availability'] for x in annotated).items()))
        print(f'Saved: {all_path}\nSaved: {json_path}')
        print(f'Archived snapshot: {archive_path}')
        print('NEXT: verify actual 90-minute game-day inactives; injury reports are not confirmation.')
        print('Do not use historical finalized injury reports as pregame data in a backtest.')
    return cards,annotated


def self_test():
    global OUT
    original_out = OUT
    from tempfile import TemporaryDirectory
    from datetime import timedelta
    with TemporaryDirectory() as d:
        base=Path(d)
        OUT = base / 'output'
        schedule=base/'sched.csv'
        tomorrow=(datetime.now(timezone.utc)+timedelta(days=1)).replace(microsecond=0)
        # schedule uses date/time kickoff from explicit UTC string
        with schedule.open('w',newline='',encoding='utf8') as f:
            w=csv.DictWriter(f,fieldnames=['season','week','game_type','home_team','away_team','gameday','gametime','gametime_utc'])
            w.writeheader();w.writerow({'season':2026,'week':6,'game_type':'REG','home_team':'KC','away_team':'LAC',
                                        'gameday':tomorrow.date().isoformat(),'gametime':'13:00',
                                        'gametime_utc':tomorrow.isoformat()})
        players=base/'players.csv'
        save_rows(players,[{'season':'2026','week':'6','game':'LAC @ KC','player':'Alpha Runner','team':'KC','opponent':'LAC',
                           'position':'RB','phase7_probability':'0.72','weather_flags':'WIND TEST','inside5_rush_last4':'4'},
                          {'season':'2026','week':'6','game':'LAC @ KC','player':'Beta Receiver','team':'KC',
                           'position':'WR','phase7_probability':'0.46','weather_flags':'WIND TEST'},
                          {'season':'2026','week':'6','game':'LAC @ KC','player':'Gamma Receiver','team':'LAC',
                           'position':'WR','phase7_probability':'0.40','weather_flags':'WIND TEST'}])
        injuries=base/'injury.csv'
        with injuries.open('w',encoding='utf8',newline='') as f:
            w=csv.DictWriter(f,fieldnames=['season','week','team','full_name','report_status',
                                           'report_primary_injury','practice_status','date_modified'])
            w.writeheader()
            w.writerows([{'season':'2026','week':'6','team':'KC','full_name':'Alpha Runner',
                          'report_status':'Out','report_primary_injury':'knee','date_modified':'2026-10-15T20:00:00Z'},
                         {'season':'2026','week':'6','team':'KC','full_name':'Beta Receiver',
                          'report_status':'Questionable','practice_status':'DNP','date_modified':'2026-10-15T20:00:00Z'},
                         {'season':'2026','week':'5','team':'LAC','full_name':'Gamma Receiver',
                          'report_status':'Out','date_modified':'2026-10-10T20:00:00Z'}])
        overrides=base/'overrides.csv'
        # Unrelated week should not alter test. Manual day-of status changes only current week.
        with overrides.open('w',encoding='utf8',newline='') as f:
            w=csv.DictWriter(f,fieldnames=OVERRIDE_COLUMNS);w.writeheader()
            w.writerow({'season':2026,'week':5,'team':'KC','player':'Beta Receiver','status':'INACTIVE'})
        report,allp=run(2026,week=6,injury_csv=injuries,player_csv=players,
                        schedule_csv=schedule,overrides_csv=overrides,quiet=True)
        assert len(report['games'])==1
        match=report['games'][0]
        assert [v['name'] for v in match['favorites']]==['Beta Receiver','Gamma Receiver']
        assert match['excluded'][0]['name']=='Alpha Runner'
        assert allp[1]['reported_game_status']=='QUESTIONABLE'
        assert allp[1]['phase7_probability']=='0.46'
        assert allp[2]['availability']=='UNKNOWN / NOT VERIFIED'
        assert report['games'][0]['weather']['weather_flags']=='WIND TEST'
        # Now a manual game-day inactive supersedes the report and excludes Beta.
        with overrides.open('a',encoding='utf8',newline='') as f:
            w=csv.writer(f);w.writerow([2026,6,'KC','Beta Receiver','INACTIVE','Official inactive checked','2026-10-18T15:00:00Z'])
        report,allp=run(2026,week=6,injury_csv=injuries,player_csv=players,
                        schedule_csv=schedule,overrides_csv=overrides,quiet=True)
        assert [v['name'] for v in report['games'][0]['favorites']]==['Gamma Receiver']
        assert len(report['games'][0]['excluded'])==2
        # unavailable injury feed must never silently mark healthy
        report2,allp2=run(2026,week=6,offline=True,player_csv=players,
                          schedule_csv=schedule,overrides_csv=base/'blank.csv',quiet=True)
        assert all('UNKNOWN' in v['availability'] for v in allp2)
    OUT = original_out
    print('SELF-TEST PASS: 2026 week filter, report statuses, override priority, '
          'no false active labels, P7 probabilities unchanged, app JSON.')


def main():
    ap=argparse.ArgumentParser(description='Phase 9: Injury-status overlay + app-ready game JSON')
    ap.add_argument('--season',type=int,default=2026)
    ap.add_argument('--week',type=int)
    ap.add_argument('--self-test',action='store_true')
    ap.add_argument('--offline',action='store_true',help='Skip injury download, mark report status UNKNOWN')
    ap.add_argument('--injuries-csv',help='Use a local injuries CSV with nflverse columns')
    ap.add_argument('--player-csv',help='Use an existing Phase 8 player CSV')
    ap.add_argument('--schedule-csv',help='Offline local NFL schedule CSV')
    ap.add_argument('--overrides-csv',help='Optional verified official game-day status CSV')
    args=ap.parse_args()
    try:
        if args.self_test:
            self_test()
        else:
            run(args.season,args.week,args.injuries_csv,args.player_csv,
                args.schedule_csv,args.overrides_csv,args.offline)
    except (OSError,ValueError,RuntimeError,csv.Error) as e:
        print(f'ERROR: {e}',file=sys.stderr)
        raise SystemExit(1)

if __name__=='__main__':
    main()
