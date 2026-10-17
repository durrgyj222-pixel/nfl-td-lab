"""NFL TD Probability Lab v1 — Python stdlib only.

Educational sports analytics model. Its estimated probabilities have NOT been
calibrated or verified as predictive. No real-money betting is advised.

Examples:
    py td_model.py --demo
    py td_model.py --live --season 2026
    py td_model.py --live --season 2026 --odds-csv your_odds.csv
    py td_model.py --live --season 2026 --probabilities-only
"""

import argparse
import csv
import io
import json
import math
import os
import re
import ssl
import sys
import time
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
CACHE = HERE / "cache"
OUTPUT = HERE / "output"
SPORT = "americanfootball_nfl"
ODDS_API = "https://api.the-odds-api.com/v4/sports/" + SPORT
NFLVERSE_URL = "https://github.com/nflverse/nflverse-data/releases/download/stats_player/stats_player_week_{season}.csv"

# Bookmaker event names to nflverse team abbreviations.
TEAM_MAP = {
    "Arizona Cardinals": "ARI", "Atlanta Falcons": "ATL", "Baltimore Ravens": "BAL",
    "Buffalo Bills": "BUF", "Carolina Panthers": "CAR", "Chicago Bears": "CHI",
    "Cincinnati Bengals": "CIN", "Cleveland Browns": "CLE", "Dallas Cowboys": "DAL",
    "Denver Broncos": "DEN", "Detroit Lions": "DET", "Green Bay Packers": "GB",
    "Houston Texans": "HOU", "Indianapolis Colts": "IND", "Jacksonville Jaguars": "JAX",
    "Kansas City Chiefs": "KC", "Las Vegas Raiders": "LV", "Los Angeles Chargers": "LAC",
    "Los Angeles Rams": "LA", "Miami Dolphins": "MIA", "Minnesota Vikings": "MIN",
    "New England Patriots": "NE", "New Orleans Saints": "NO", "New York Giants": "NYG",
    "New York Jets": "NYJ", "Philadelphia Eagles": "PHI", "Pittsburgh Steelers": "PIT",
    "San Francisco 49ers": "SF", "Seattle Seahawks": "SEA", "Tampa Bay Buccaneers": "TB",
    "Tennessee Titans": "TEN", "Washington Commanders": "WAS",
}
TEAM_ALIASES = {"LAR": "LA", "WSH": "WAS", "JAC": "JAX", "OAK": "LV", "SD": "LAC", "STL": "LA"}


def clean_team(team):
    t = str(team or "").strip().upper()
    return TEAM_ALIASES.get(t, t)


def clean_name(value):
    s = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode()
    s = s.lower().replace("’", "'")
    s = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", s)
    return re.sub(r"[^a-z0-9]", "", s)


def american_to_decimal(odds):
    n = int(str(odds).replace("+", "").strip())
    if n == 0 or -100 < n < 100:
        raise ValueError(f"Invalid American odds: {odds!r}")
    return 1 + (n / 100 if n > 0 else 100 / abs(n))


def fair_odds(prob):
    if not 0 < prob < 1:
        return "N/A"
    if prob >= 0.5:
        return f"-{round(100 * prob / (1-prob))}"
    return f"+{round(100 * (1-prob) / prob)}"


def inferred_season():
    d = datetime.now(timezone.utc)
    return d.year if d.month >= 7 else d.year - 1


def request_bytes(url, cache_name, seconds, headers=None):
    CACHE.mkdir(exist_ok=True)
    target = CACHE / cache_name
    if target.exists() and time.time() - target.stat().st_mtime < seconds:
        return target.read_bytes()
    req = Request(url, headers={"User-Agent": "TD-Probability-Lab/1.0", **(headers or {})})
    try:
        try:
            import certifi
            context = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            context = ssl.create_default_context()
        with urlopen(req, timeout=25, context=context) as response:
            data = response.read()
            quota = response.headers.get("x-requests-remaining")
            if quota is not None:
                print(f"  Provider credits remaining: {quota}")
    except HTTPError as exc:
        detail = exc.read(400).decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {exc.code} from data provider. {detail[:250]}") from exc
    except (URLError, TimeoutError) as exc:
        if target.exists():
            print(f"  WARNING: provider unavailable; using older cached {cache_name}")
            return target.read_bytes()
        raise RuntimeError(f"Network download failed: {exc}") from exc
    target.write_bytes(data)
    return data


def json_get(url, cache_name, age_secs):
    return json.loads(request_bytes(url, cache_name, age_secs).decode("utf-8"))


def nfl_stats(season, local_csv=None):
    if local_csv:
        print(f"Reading local weekly stats: {local_csv}")
        with Path(local_csv).open("r", encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))
    url = NFLVERSE_URL.format(season=season)
    print(f"Downloading nflverse {season} weekly player stats...")
    data = request_bytes(url, f"stats_player_week_{season}.csv", 6 * 3600)
    rows = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))
    if not rows:
        raise RuntimeError("The NFL player stats download was empty.")
    return rows


def num(row, *keys):
    for k in keys:
        v = row.get(k)
        if v not in (None, "", "NA", "NaN"):
            try:
                return float(v)
            except ValueError:
                continue
    return 0.0


def team_and_player_rates(raw_rows, through_week=None):
    """Rough team TD and player opportunity shares, excluding future games.

    This deliberately does NOT use the market price in its forecast.
    """
    team_weeks = defaultdict(set)
    teams = defaultdict(lambda: defaultdict(float))
    team_by_week = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    player_rows = defaultdict(list)
    for row in raw_rows:
        season_type = (row.get("season_type") or row.get("game_type") or "REG").upper()
        if season_type not in ("REG", "REGULAR"):
            continue
        week = int(num(row, "week"))
        if week < 1 or (through_week is not None and week > through_week):
            continue
        team = clean_team(row.get("team") or row.get("recent_team"))
        if not team:
            continue
        name = row.get("player_display_name") or row.get("player_name")
        pid = row.get("player_id") or name
        if not name or not pid:
            continue
        team_weeks[team].add(week)
        metrics = {
            "carries": num(row, "carries"), "targets": num(row, "targets"),
            "rushing_tds": num(row, "rushing_tds"), "receiving_tds": num(row, "receiving_tds"),
        }
        for k, v in metrics.items():
            teams[team][k] += v
            team_by_week[team][week][k] += v
        player_rows[(team, str(pid))].append({
            "name": str(name).strip(), "position": row.get("position") or "?",
            "team": team, "week": week, **metrics
        })

    if not team_weeks:
        raise RuntimeError("No usable regular-season weekly stats. Verify CSV columns/season.")

    total_games = sum(len(x) for x in team_weeks.values())
    avg_rush = sum(t["rushing_tds"] for t in teams.values()) / total_games
    avg_rec = sum(t["receiving_tds"] for t in teams.values()) / total_games
    result = []
    for (team, pid), games in player_rows.items():
        games.sort(key=lambda r: r["week"])
        weeks = sorted(team_weeks[team])
        last4 = set(weeks[-4:])
        t = teams[team]
        prior = 3.0  # stabilize early-season team touchdown rates
        team_rush_rate = (t["rushing_tds"] + prior * avg_rush) / (len(weeks) + prior)
        team_rec_rate = (t["receiving_tds"] + prior * avg_rec) / (len(weeks) + prior)
        last_totals = {k: sum(team_by_week[team][w][k] for w in last4)
                       for k in ("carries", "targets")}
        p = {k: sum(g[k] for g in games) for k in ("carries", "targets", "rushing_tds", "receiving_tds")}
        recent = {k: sum(g[k] for g in games if g["week"] in last4) for k in ("carries", "targets")}

        def opportunity(k):
            season_share = p[k] / t[k] if t[k] else 0
            recent_share = recent[k] / last_totals[k] if last_totals[k] else season_share
            return 0.65 * season_share + 0.35 * recent_share

        rush_share = opportunity("carries")
        rec_share = opportunity("targets")
        # Observed TD shares are highly noisy; blend them lightly with usage.
        prior_tds = 5.0
        rush_td_share = (p["rushing_tds"] + prior_tds * rush_share) / (t["rushing_tds"] + prior_tds)
        rec_td_share = (p["receiving_tds"] + prior_tds * rec_share) / (t["receiving_tds"] + prior_tds)
        rush_allocation = 0.75 * rush_share + 0.25 * rush_td_share
        rec_allocation = 0.75 * rec_share + 0.25 * rec_td_share
        estimated_tds = team_rush_rate * rush_allocation + team_rec_rate * rec_allocation
        probability = 1 - math.exp(-estimated_tds)
        if p["carries"] + p["targets"] <= 0:
            continue
        result.append({
            "player": games[-1]["name"], "player_key": clean_name(games[-1]["name"]),
            "team": team, "position": games[-1]["position"], "season_games_team": len(weeks),
            "player_games": len({g["week"] for g in games}),
            "season_carries": int(p["carries"]), "season_targets": int(p["targets"]),
            "rush_usage_share": rush_share, "target_share": rec_share,
            "expected_tds": estimated_tds, "model_probability": probability,
        })
    return result


def parse_event_anytime_odds(event, data):
    """Keep the highest numeric Yes price per (player, event)."""
    out = {}
    allowed_teams = {clean_team(TEAM_MAP.get(event.get("home_team"), "")),
                     clean_team(TEAM_MAP.get(event.get("away_team"), ""))}
    allowed_teams.discard("")
    for book in data.get("bookmakers", []):
        for market in book.get("markets", []):
            if market.get("key") != "player_anytime_td":
                continue
            for outcome in market.get("outcomes", []):
                name = str(outcome.get("name", "")).strip()
                desc = str(outcome.get("description", "")).strip()
                if name.lower() in ("no", "under"):
                    continue
                if name.lower() in ("yes", "over"):
                    if name.lower() == "over" and outcome.get("point") not in (None, 0.5):
                        continue
                    player_name = desc
                else:
                    # Some feeds place the player name directly in 'name'.
                    player_name = name if not desc else desc
                key = clean_name(player_name)
                if not key or key in ("yes", "no", "over", "under"):
                    continue
                try:
                    odds = int(outcome["price"])
                    dec = american_to_decimal(odds)
                except (KeyError, TypeError, ValueError):
                    continue
                if key not in out or dec > out[key]["decimal_odds"]:
                    out[key] = {
                        "player_key": key, "player_in_odds": player_name,
                        "team_choices": allowed_teams,
                        "american_odds": odds, "decimal_odds": dec,
                        "book": book.get("title") or book.get("key", "Unknown"),
                        "opponent": f"{event.get('away_team', '?')} @ {event.get('home_team', '?')}",
                        "kickoff_utc": event.get("commence_time", ""),
                        "price_updated_utc": market.get("last_update") or book.get("last_update", ""),
                    }
    return list(out.values())


def odds_from_live(key, days, max_events):
    params = urlencode({"apiKey": key})
    events = json_get(ODDS_API + "/events?" + params, "events.json", 10 * 60)
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=days)
    future = []
    for e in events:
        try:
            start = datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if now <= start <= cutoff:
            future.append(e)
    future.sort(key=lambda e: e["commence_time"])
    future = future[:max_events]
    print(f"Found {len(future)} upcoming games in the next {days} days.")
    print("Each uncached game's anytime-TD request uses API credits.")
    outcomes = []
    for i, event in enumerate(future, 1):
        print(f"  [{i}/{len(future)}] {event['away_team']} @ {event['home_team']}")
        props = urlencode({"apiKey": key, "regions": "us", "markets": "player_anytime_td",
                           "oddsFormat": "american"})
        url = ODDS_API + "/events/" + event["id"] + "/odds?" + props
        data = json_get(url, "props_" + re.sub(r"[^a-zA-Z0-9]", "", event["id"]) + ".json", 20 * 60)
        outcomes.extend(parse_event_anytime_odds(event, data))
    return outcomes


def odds_from_csv(path):
    out = []
    with Path(path).open("r", newline="", encoding="utf-8-sig") as handle:
        for i, row in enumerate(csv.DictReader(handle), start=2):
            try:
                dec = american_to_decimal(row["american_odds"])
            except (ValueError, KeyError) as exc:
                print(f"Skipping odds CSV row {i}: {exc}")
                continue
            team = clean_team(row.get("team"))
            out.append({
                "player_key": clean_name(row["player"]), "player_in_odds": row["player"],
                "team_choices": {team} if team else set(),
                "american_odds": int(str(row["american_odds"]).replace("+", "")),
                "decimal_odds": dec, "book": row.get("book") or "Manual",
                "opponent": row.get("game") or "Manual", "kickoff_utc": row.get("kickoff_utc") or "",
                "price_updated_utc": row.get("price_updated_utc") or "",
            })
    return out


def combine(model_rows, odds_rows):
    by_name = defaultdict(list)
    for row in model_rows:
        by_name[row["player_key"]].append(row)
    ranked, unmatched = [], []
    for quote in odds_rows:
        candidates = [r for r in by_name[quote["player_key"]]
                      if not quote["team_choices"] or r["team"] in quote["team_choices"]]
        if len(candidates) != 1:
            unmatched.append(quote["player_in_odds"])
            continue
        m = candidates[0]
        prob = m["model_probability"]
        implied = 1 / quote["decimal_odds"]
        ev = prob * (quote["decimal_odds"] - 1) - (1 - prob)
        ranked.append({
            **{k: v for k, v in m.items() if k != "player_key"},
            **{k: v for k, v in quote.items() if k not in ("player_key", "player_in_odds", "team_choices")},
            "model_probability": prob, "market_implied_probability": implied,
            "edge_percentage_points": (prob - implied) * 100,
            "estimated_roi": ev,
            "model_fair_odds": fair_odds(prob),
        })
    ranked.sort(key=lambda r: r["estimated_roi"], reverse=True)
    return ranked, unmatched


def save_csv(rows, basename):
    OUTPUT.mkdir(exist_ok=True)
    target = OUTPUT / basename
    if rows:
        preferred = ["player", "team", "position", "opponent", "kickoff_utc", "model_probability",
                     "american_odds", "book", "market_implied_probability", "edge_percentage_points",
                     "estimated_roi", "model_fair_odds", "expected_tds", "season_carries", "season_targets",
                     "rush_usage_share", "target_share", "season_games_team", "player_games",
                     "price_updated_utc"]
        fields = [f for f in preferred if f in rows[0]]
        with target.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        print(f"Saved: {target}")
    else:
        print("Nothing to save: zero matching rows.")


def display(rows, only_probabilities=False):
    if only_probabilities:
        print("\nMOST LIKELY TD SCORERS (UNVALIDATED MODEL, NO ODDS)\n")
        print(f"{'Player':<24} {'Team':<5} {'Pos':<4} {'Model TD %':>10} {'Carries':>9} {'Targets':>9}")
        for r in sorted(rows, key=lambda x: x["model_probability"], reverse=True)[:30]:
            print(f"{r['player'][:24]:<24} {r['team']:<5} {r['position']:<4} "
                  f"{r['model_probability']:>9.1%} {r['season_carries']:>9} {r['season_targets']:>9}")
        return
    print("\nANYTIME TD LAB — ESTIMATED VALUE RANKINGS (EXPERIMENTAL)\n")
    print(f"{'Player':<23} {'Team':<5} {'Odds':>6} {'Model':>7} {'Implied':>8} {'Edge':>9} {'Model EV':>9}  Book")
    print("-" * 96)
    for r in rows[:30]:
        print(f"{r['player'][:23]:<23} {r['team']:<5} {r['american_odds']:>+6} "
              f"{r['model_probability']:>6.1%} {r['market_implied_probability']:>8.1%} "
              f"{r['edge_percentage_points']:>+8.1f}pp {r['estimated_roi']:>+8.1%}  {r['book']}")


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="NFL weekly TD probability lab, stdlib only")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--demo", action="store_true", help="Run with local fictional examples; no internet/key")
    mode.add_argument("--live", action="store_true", help="Pull live stats and, if configured, odds")
    ap.add_argument("--season", type=int, default=inferred_season(), help="NFL season year")
    ap.add_argument("--through-week", type=int, help="Max historical week used (for testing/backtests)")
    ap.add_argument("--days", type=int, default=4, help="Search upcoming games this many days out")
    ap.add_argument("--max-events", type=int, default=16, help="Max prop events to request (limits API credits)")
    ap.add_argument("--stats-csv", help="Optional local nflverse-format weekly stats file")
    ap.add_argument("--odds-csv", help="Optional manually collected American-odds CSV; skips odds API")
    ap.add_argument("--probabilities-only", action="store_true", help="Skip odds; rank raw model probabilities")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if not (1 <= args.days <= 14 and 1 <= args.max_events <= 32):
        raise RuntimeError("--days must be 1-14 and --max-events must be 1-32")
    if args.demo:
        print("\nDEMO MODE — EVERYTHING IS FICTIONAL. No live NFL stats or prices.\n")
        stats_path = HERE / "demo_stats.csv"
        odds_path = HERE / "demo_odds.csv"
    else:
        stats_path = args.stats_csv
        odds_path = args.odds_csv
    stats = nfl_stats(args.season, stats_path)
    players = team_and_player_rates(stats, args.through_week)
    print(f"Modeled {len(players)} players with recorded carries/targets.")
    if args.probabilities_only:
        display(players, only_probabilities=True)
        save_csv(players, "td_probabilities.csv")
        return
    if odds_path:
        quotes = odds_from_csv(odds_path)
    else:
        key = os.environ.get("ODDS_API_KEY", "").strip()
        if not key:
            raise RuntimeError("Missing ODDS_API_KEY. Set it in Command Prompt, provide --odds-csv, or use --probabilities-only.")
        quotes = odds_from_live(key, args.days, args.max_events)
    ranks, unmatched = combine(players, quotes)
    print(f"Parsed {len(quotes)} player odds; matched {len(ranks)} player quotes.")
    if unmatched:
        seen = list(dict.fromkeys(unmatched))
        print(f"Unmatched player names ({len(seen)}): {', '.join(seen[:15])}")
        print("(Rookies, injuries, missing stats, and spelling differences need review.)")
    if ranks:
        display(ranks)
    else:
        print("No matches to rank. Odds may be missing or players not in current stats.")
    save_csv(ranks, "td_rankings.csv")
    print("\nWARNING: Model is not calibrated or backtested. A positive estimated")
    print("edge is not evidence of a real advantage. Verify prices and availability.")
    print("Passing TDs thrown by a QB do not count as anytime TD scores.\n")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, csv.Error, ValueError) as exc:
        print(f"\nERROR: {exc}")
        sys.exit(1)
