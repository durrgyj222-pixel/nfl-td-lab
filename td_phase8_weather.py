"""NFL TD Lab — Phase 8: game-time weather overlay (NO probability adjustment).

Install beside td_weekly.py and td_model.py. Run:
  py td_phase8_weather.py --self-test
  py td_phase8_weather.py --season 2026

Reads EXISTING output/phase7_week_<week>_<season>.csv and upcoming nflverse schedule.
Uses Open-Meteo forecast for the actual STADIUM location at kickoff and over the
first approximately 3 hours. Never adjusts TD probabilities based on untested
weather coefficients. Unknown venues are reported as UNKNOWN, not guessed.

Standard library + existing certifi for Windows SSL. Attribution: Open-Meteo.
"""
import argparse
import csv
import json
import ssl
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import td_weekly as weekly
import td_model as model

BASE = Path(__file__).resolve().parent
OUTPUT = BASE / 'output'
API = 'https://api.open-meteo.com/v1/forecast'
HOURLY = ('temperature_2m,precipitation_probability,precipitation,rain,'
          'snowfall,wind_speed_10m,wind_gusts_10m,weather_code')

# Stadium-specific locations are essential for neutral-site/international games.
# Coords approximate stadium centers, for weather GRID lookup, not precision GPS.
# flag: outdoors, indoor, retractable, canopy, or unknown.
VENUES = [
    ('wembley', 51.5560, -0.2796, 'outdoors'),
    ('tottenham hotspur', 51.6043, -0.0662, 'outdoors'),
    ('allianz arena', 48.2188, 11.6247, 'outdoors'),
    ('santiago bernab', 40.4531, -3.6883, 'retractable'),
    ('olympiastadion berlin', 52.5147, 13.2394, 'outdoors'),
    ('croke park', 53.3607, -6.2513, 'outdoors'),
    ('azteca', 19.3029, -99.1505, 'outdoors'),
    ('corinthians', -23.5456, -46.4732, 'outdoors'),
    ('state farm stadium', 33.5276, -112.2626, 'retractable'),
    ('mercedes-benz stadium', 33.7554, -84.4009, 'retractable'),
    ('m&t bank stadium', 39.2780, -76.6227, 'outdoors'),
    ('highmark stadium', 42.7738, -78.7868, 'outdoors'),
    ('bank of america stadium', 35.2258, -80.8528, 'outdoors'),
    ('soldier field', 41.8623, -87.6167, 'outdoors'),
    ('paycor stadium', 39.0954, -84.5160, 'outdoors'),
    ('paul brown stadium', 39.0954, -84.5160, 'outdoors'),
    ('huntington bank field', 41.5061, -81.6995, 'outdoors'),
    ('firstenergy stadium', 41.5061, -81.6995, 'outdoors'),
    ('cleveland browns stadium', 41.5061, -81.6995, 'outdoors'),
    ('at&t stadium', 32.7473, -97.0945, 'retractable'),
    ('empower field', 39.7439, -105.0201, 'outdoors'),
    ('ford field', 42.3400, -83.0456, 'indoor'),
    ('lambeau field', 44.5013, -88.0622, 'outdoors'),
    ('nrg stadium', 29.6847, -95.4107, 'retractable'),
    ('lucas oil stadium', 39.7601, -86.1639, 'retractable'),
    ('everbank stadium', 30.3240, -81.6373, 'outdoors'),
    ('tiaa bank field', 30.3240, -81.6373, 'outdoors'),
    ('arrowhead stadium', 39.0489, -94.4839, 'outdoors'),
    ('geha field', 39.0489, -94.4839, 'outdoors'),
    ('allegiant stadium', 36.0909, -115.1833, 'indoor'),
    ('sofi stadium', 33.9535, -118.3392, 'canopy'),
    ('hard rock stadium', 25.9580, -80.2389, 'canopy'),
    ('u.s. bank stadium', 44.9737, -93.2579, 'indoor'),
    ('us bank stadium', 44.9737, -93.2579, 'indoor'),
    ('gillette stadium', 42.0909, -71.2643, 'outdoors'),
    ('caesars superdome', 29.9511, -90.0812, 'indoor'),
    ('mercedes-benz superdome', 29.9511, -90.0812, 'indoor'),
    ('metlife stadium', 40.8135, -74.0745, 'outdoors'),
    ('lincoln financial field', 39.9008, -75.1675, 'outdoors'),
    ('acrisure stadium', 40.4468, -80.0158, 'outdoors'),
    ('heinz field', 40.4468, -80.0158, 'outdoors'),
    ("levi's stadium", 37.4030, -121.9700, 'outdoors'),
    ('lumen field', 47.5952, -122.3316, 'outdoors'),
    ('raymond james stadium', 27.9759, -82.5033, 'outdoors'),
    ('nissan stadium', 36.1665, -86.7713, 'outdoors'),
    ('northwest stadium', 38.9078, -76.8644, 'outdoors'),
    ('commanders field', 38.9078, -76.8644, 'outdoors'),
    ('fedexfield', 38.9078, -76.8644, 'outdoors'),
]


def find_venue(name):
    if not name:
        return None
    normalized = name.strip().lower().replace('\u2019', "'")
    for key, lat, lon, roof in VENUES:
        if key in normalized:
            return (lat, lon, roof)
    return None


def roof_exposure(raw, default):
    """Return stadium status; roof not confirmed for retractables pre-game."""
    roof = (raw or '').lower().strip()
    if default == 'indoor':
        return 'INDOOR'
    if default == 'canopy':
        return 'COVERED/OPEN-SIDED'
    if roof in ('closed', 'dome'):
        return 'INDOOR (schedule label)'
    if roof in ('outdoors', 'open') and default != 'retractable':
        return 'OUTDOORS'
    if default == 'retractable':
        return 'RETRACTABLE (ROOF UNCONFIRMED)'
    if roof in ('outdoors', 'open') or default == 'outdoors':
        return 'OUTDOORS'
    return 'UNKNOWN'


def fetch_weather(lat, lon, kickoff, opener=None):
    """Returns raw Open-Meteo hourly forecast; only for future games.

    No weather from after a game; never used for historical backtests.
    """
    kickoff = kickoff.astimezone(timezone.utc)
    end = kickoff + timedelta(hours=4)
    params = {
        'latitude': f'{lat:.5f}', 'longitude': f'{lon:.5f}',
        'hourly': HOURLY, 'timezone': 'UTC',
        'temperature_unit': 'fahrenheit', 'wind_speed_unit': 'mph',
        'precipitation_unit': 'inch',
        'start_date': kickoff.date().isoformat(),
        'end_date': end.date().isoformat(),
    }
    url = API + '?' + urlencode(params)
    if opener is not None:
        return opener(url)
    try:
        import certifi
        context = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        context = ssl.create_default_context()
    request = Request(url, headers={'User-Agent': 'NFL-TD-Lab-Weather/1.0 (student research)'})
    with urlopen(request, context=context, timeout=25) as response:
        return json.load(response)


def fnum(v):
    try:
        return float(v) if v not in (None, '') else None
    except (ValueError, TypeError):
        return None


def summarize_weather(data, kickoff):
    hourly = data.get('hourly') or {}
    times = hourly.get('time') or []
    if not times:
        raise ValueError('Weather API returned no hourly timestamps.')
    parsed = []
    for i, raw in enumerate(times):
        try:
            dt = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
            dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
            parsed.append((dt, i))
        except ValueError:
            continue
    if not parsed:
        raise ValueError('Weather API timestamps could not be parsed.')
    kickoff = kickoff.astimezone(timezone.utc)
    # Nearest available hour to kickoff, plus 3-hour forecast window.
    near, idx = min(parsed, key=lambda x: abs((x[0] - kickoff).total_seconds()))
    if abs((near-kickoff).total_seconds()) > 5400:
        raise ValueError('No forecast hour within 90 minutes of kickoff.')
    # Hourly precipitation is an accumulation; take the three full hour-ENDS after kickoff.
    window = [i for dt, i in parsed if timedelta(0) < dt-kickoff <= timedelta(hours=3)]
    if not window:
        window = [idx]

    def at(field, index):
        values = hourly.get(field, [])
        return fnum(values[index]) if index < len(values) else None

    def maximum(field):
        vals = [at(field, i) for i in window]
        vals = [v for v in vals if v is not None]
        return max(vals) if vals else None

    def summation(field):
        vals = [at(field, i) for i in window]
        vals = [v for v in vals if v is not None]
        return sum(vals) if vals else None

    result = {
        'forecast_hour_utc': near.isoformat(timespec='minutes'),
        'temperature_f': at('temperature_2m', idx),
        'precip_probability_pct_max': maximum('precipitation_probability'),
        'precipitation_3h_in': summation('precipitation'),
        'rain_3h_in': summation('rain'),
        'snowfall_3h_in': (summation('snowfall') / 2.54 if summation('snowfall') is not None else None),
        'wind_mph_max': maximum('wind_speed_10m'),
        'gust_mph_max': maximum('wind_gusts_10m'),
        'sampled_hours': len(window),
    }
    if all(result[k] is None for k in ('temperature_f', 'precip_probability_pct_max', 'wind_mph_max')):
        raise ValueError('Missing temperature/precipitation/wind values in API response.')
    return result


def condition_tags(weather, roof):
    """Descriptive context only, not a validated scoring adjustment."""
    if roof.startswith('INDOOR'):
        return 'OUTDOOR WEATHER DOES NOT DIRECTLY APPLY'
    tags = []
    rain = weather.get('rain_3h_in')
    precip = weather.get('precipitation_3h_in')
    snow = weather.get('snowfall_3h_in')
    gust = weather.get('gust_mph_max')
    temp = weather.get('temperature_f')
    if precip is not None and precip >= 0.5:
        tags.append('HEAVY PRECIPITATION FORECAST')
    elif rain is not None and rain >= 0.15:
        tags.append('RAIN FORECAST')
    if snow is not None and snow >= 0.25:
        tags.append('SNOW FORECAST')
    if gust is not None and gust >= 30:
        tags.append('STRONG WIND GUSTS')
    if temp is not None and temp <= 32:
        tags.append('FREEZING TEMPERATURE')
    if temp is not None and temp >= 90:
        tags.append('HIGH HEAT')
    if roof.startswith('RETRACTABLE'):
        tags.append('CHECK ROOF STATUS')
    if roof == 'COVERED/OPEN-SIDED':
        tags.append('PARTIALLY SHELTERED VENUE')
    if roof == 'UNKNOWN':
        tags.append('EXPOSURE UNKNOWN')
    return ' | '.join(tags) if tags else 'NO MAJOR WEATHER FLAGS'


def fmt(v, suffix='', digits=0):
    if v is None:
        return 'n/a'
    return f'{v:.{digits}f}{suffix}'


def load_csv(path):
    with Path(path).open('r', encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def write_csv(path, rows, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)


def attached_metadata(schedules, game, season):
    for r in schedules:
        try:
            y, w = int(r.get('season') or -1), int(r.get('week') or -1)
        except ValueError:
            continue
        if y != season or w != game['week']:
            continue
        home = model.clean_team(r.get('home_team'))
        away = model.clean_team(r.get('away_team'))
        if home == game['home'] and away == game['away']:
            return r
    return {}


def run(season, week=None, forecast_loader=None, schedule_csv=None, player_csv=None,
        now=None, emit=True):
    now = now or datetime.now(timezone.utc)
    schedule = weekly.load_schedule(schedule_csv)
    chosen_week, games = weekly.upcoming_games(schedule, season, now=now, week=week)
    if not games:
        raise RuntimeError('No remaining upcoming games for that week in schedule.')
    if player_csv is None:
        player_csv = OUTPUT / f'phase7_week_{chosen_week}_{season}.csv'
    if not Path(player_csv).is_file():
        raise RuntimeError(f'Missing player report: {player_csv}\n'
                           f'First run: py td_phase7_fixed.py --weekly --season {season} --train-season 2024')
    players = load_csv(player_csv)
    if 'phase7_probability' not in (players[0] if players else {}):
        raise RuntimeError('Input CSV is missing phase7_probability; use Phase 7 weekly output.')
    fetched_at = datetime.now(timezone.utc).isoformat(timespec='seconds')
    forecasts = []
    merged = []
    if emit:
        print(f'\nPHASE 8: WEEK {chosen_week} ({season}) — VENUE-SPECIFIC KICKOFF WEATHER')
        print('Weather is information ONLY: Phase 7 scoring probabilities remain unchanged.')
    for game in games:
        title = f"{game['away']} @ {game['home']}"
        info = attached_metadata(schedule, game, season)
        stadium = (info.get('stadium') or '').strip()
        venue = find_venue(stadium)
        roof = 'UNKNOWN'
        weather = {}
        error = ''
        location_note = ''
        if not venue:
            error = f'Unrecognized or missing stadium: {stadium or "(blank)"}; no home-city substitution.'
            location_note = 'UNRESOLVED VENUE'
        else:
            lat, lon, default = venue
            roof = roof_exposure(info.get('roof'), default)
            location_note = 'STADIUM MATCH'
            if roof.startswith('INDOOR'):
                error = 'Skipped outdoor weather fetch: enclosed venue.'
            else:
                try:
                    payload = (forecast_loader(lat, lon, game['start']) if forecast_loader
                               else fetch_weather(lat, lon, game['start']))
                    weather = summarize_weather(payload, game['start'])
                except Exception as e:
                    error = f'Forecast unavailable: {str(e)[:180]}'
        flag = condition_tags(weather, roof) if weather else ('INDOOR' if roof.startswith('INDOOR') else 'WEATHER UNAVAILABLE')
        entry = {
            'season': season, 'week': chosen_week, 'game': title,
            'kickoff_utc': game['start'].isoformat(timespec='minutes'),
            'kickoff_eastern': f"{game['day']} {game['time_eastern']} ET",
            'stadium': stadium or 'UNKNOWN', 'roof': roof,
            'latitude': venue[0] if venue else '', 'longitude': venue[1] if venue else '',
            'weather_source': 'Open-Meteo forecast' if weather else '',
            'retrieved_at_utc': fetched_at, 'location_match': location_note,
            **weather, 'weather_flags': flag, 'note': error,
        }
        forecasts.append(entry)
        group = sorted([p for p in players if p.get('game') == title],
                       key=lambda r: -float(r.get('phase7_probability') or 0))
        for p in group:
            merged.append({**p, **{k:entry.get(k,'') for k in ('kickoff_utc','stadium','roof','temperature_f',
                          'precip_probability_pct_max','precipitation_3h_in','rain_3h_in',
                          'snowfall_3h_in','wind_mph_max','gust_mph_max','weather_flags',
                          'retrieved_at_utc','weather_source')}})
        if emit:
            print(f'\n  {entry["kickoff_eastern"]} | {title} | {entry["stadium"]}')
            print(f'  Roof/exposure: {roof}')
            if weather:
                print(f'  Approx kickoff {fmt(weather.get("temperature_f"),"F")}, '
                      f'precip chance <=3h max {fmt(weather.get("precip_probability_pct_max"),"%")}, '
                      f'precip total ~{fmt(weather.get("precipitation_3h_in")," in",2)}')
                print(f'  Wind max {fmt(weather.get("wind_mph_max")," mph")}; '
                      f'gust max {fmt(weather.get("gust_mph_max")," mph")}; '
                      f'snow total ~{fmt(weather.get("snowfall_3h_in")," in",2)}')
            elif error:
                print(f'  {error}')
            print(f'  {flag}')
            for p in group[:3]:
                print(f'   {p.get("player", "")[:25]:25s}  Phase7 {float(p["phase7_probability"]):5.1%} (UNCHANGED)')
            if not group:
                print('  NOTE: No player rows matched this game; review Phase 7 report.')
    basename = f'phase8_week_{chosen_week}_{season}'
    all_weather_keys = list(dict.fromkeys(k for rec in forecasts for k in rec))
    player_fields = list(dict.fromkeys(k for rec in merged for k in rec))
    path1 = OUTPUT / (basename+'_weather.csv')
    path2 = OUTPUT / (basename+'_players.csv')
    write_csv(path1, forecasts, all_weather_keys)
    if merged:
        write_csv(path2, merged, player_fields)
    if emit:
        print(f'\nSaved: {path1}')
        if merged: print(f'Saved: {path2}')
        print('SOURCE: Open-Meteo forecast + nflverse schedule stadium metadata.')
        print('DISCLAIMERS: venue forecasts are approximate, roofs may change, predictions are unvalidated.')
        print('Historical weather used after kickoff would leak future information into a backtest.')
    return forecasts, merged


def self_test():
    assert find_venue('Wembley Stadium') == (51.556, -0.2796, 'outdoors')
    assert find_venue('Nonexistent Arena') is None
    assert roof_exposure('', 'retractable') == 'RETRACTABLE (ROOF UNCONFIRMED)'
    assert roof_exposure('dome', 'outdoors').startswith('INDOOR')
    assert roof_exposure('', 'indoor') == 'INDOOR'
    kickoff = datetime(2026, 10, 18, 13, 30, tzinfo=timezone.utc)
    data = {'hourly': {
        'time': ['2026-10-18T13:00','2026-10-18T14:00','2026-10-18T15:00','2026-10-18T16:00'],
        'temperature_2m': [47,48,49,50],
        'precipitation_probability': [40,85,70,30],
        'precipitation':[.1,.25,.2,.05], 'rain':[.1,.25,.2,.05],
        'snowfall':[0,0,0,0], 'wind_speed_10m':[15,22,20,12],
        'wind_gusts_10m':[20,35,28,19]}}
    summary = summarize_weather(data, kickoff)
    assert summary['temperature_f'] == 47
    assert summary['precip_probability_pct_max'] == 85
    assert summary['precipitation_3h_in'] > .4
    assert 'STRONG WIND GUSTS' in condition_tags(summary, 'OUTDOORS')
    assert 'DOES NOT DIRECTLY APPLY' in condition_tags(summary, 'INDOOR')
    print('SELF-TEST PASS: Wembley venue, roof handling, UTC kickoff, hourly window, weather flags.')


def main():
    ap = argparse.ArgumentParser(description='Phase 8: stadium weather overlay for Week N')
    ap.add_argument('--season', type=int, default=2026)
    ap.add_argument('--week', type=int, default=None)
    ap.add_argument('--self-test', action='store_true')
    ap.add_argument('--schedule-csv')
    ap.add_argument('--player-csv')
    args = ap.parse_args()
    if args.self_test:
        self_test()
        return
    try:
        run(args.season, week=args.week, schedule_csv=args.schedule_csv, player_csv=args.player_csv)
    except (ValueError, RuntimeError, OSError) as e:
        print(f'ERROR: {e}', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
