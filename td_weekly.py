"""NFL TD Lab Phase 2: rank only players with upcoming games.

Place this beside your working td_model.py. Requires your existing certifi SSL fix.
Run: py td_weekly.py --season 2026
No sportsbook data or API key required. Educational, unvalidated probabilities.
"""
import argparse
import csv
import io
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
import sys

import td_model as model

SCHEDULE_URL = "https://github.com/nflverse/nflverse-data/releases/download/schedules/games.csv"
OUTPUT = Path(__file__).resolve().parent / "output"


def load_schedule(path=None):
    if path:
        with open(path, encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))
    print("Downloading nflverse NFL schedule...")
    data = model.request_bytes(SCHEDULE_URL, "nfl_schedules_games.csv", 6 * 3600)
    return list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))


def nth_sunday(year, month, number):
    first = date(year, month, 1)
    days_until_sunday = (6 - first.weekday()) % 7
    return first + timedelta(days=days_until_sunday + 7 * (number - 1))


def kickoff_utc(row):
    """Parse nflverse gameday/gametime, converting U.S. Eastern kickoff to UTC.

    Kickoffs don't happen during the 2am DST transition, so date-based rules suffice.
    If time is unspecified, do not exclude the game until the following date.
    """
    try:
        day = date.fromisoformat(str(row.get("gameday") or row.get("game_date"))[:10])
    except (TypeError, ValueError):
        return None
    raw_time = (row.get("gametime") or "").strip()
    if not raw_time:
        return datetime.combine(day, time(23, 59), tzinfo=timezone.utc)
    try:
        local_clock = time.fromisoformat(raw_time)
    except ValueError:
        return datetime.combine(day, time(23, 59), tzinfo=timezone.utc)
    # EST is UTC-5; EDT is UTC-4.
    dst = nth_sunday(day.year, 3, 2) <= day < nth_sunday(day.year, 11, 1)
    offset = timedelta(hours=4 if dst else 5)
    return datetime.combine(day, local_clock, tzinfo=timezone.utc) + offset


def upcoming_games(schedule, season, now=None, week=None):
    now = now or datetime.now(timezone.utc)
    games = []
    for row in schedule:
        try:
            s = int(float(row.get("season", "")))
            w = int(float(row.get("week", "")))
        except (ValueError, TypeError):
            continue
        if s != season or (row.get("game_type") or "REG").upper() != "REG":
            continue
        if week is not None and w != week:
            continue
        start = kickoff_utc(row)
        if start is None or start <= now:
            continue
        home, away = model.clean_team(row.get("home_team")), model.clean_team(row.get("away_team"))
        if not home or not away:
            continue
        games.append({"week": w, "home": home, "away": away,
                      "start": start, "day": (row.get("gameday") or "")[:10],
                      "time_eastern": row.get("gametime") or "TBD"})
    if not games:
        return None, []
    first_week = week if week is not None else min(games, key=lambda g: g["start"])["week"]
    return first_week, sorted([g for g in games if g["week"] == first_week], key=lambda g: g["start"])


def current_team_for_player(raw_rows, through_week):
    """Heuristic: a player's most recently recorded team, avoiding old team duplicates."""
    latest = {}
    for row in raw_rows:
        if (row.get("season_type") or row.get("game_type") or "REG").upper() != "REG":
            continue
        try:
            w = int(float(row.get("week") or 0))
        except ValueError:
            continue
        if w > through_week or w <= 0:
            continue
        name = row.get("player_display_name") or row.get("player_name")
        team = model.clean_team(row.get("team") or row.get("recent_team"))
        if not name or not team:
            continue
        key = model.clean_name(name)
        usage = model.num(row, "carries") + model.num(row, "targets")
        if key not in latest or (w, usage) > latest[key][:2]:
            latest[key] = (w, usage, team)
    return {k: v[2] for k, v in latest.items()}


def run(argv=None):
    ap = argparse.ArgumentParser(description="TD Lab Phase 2: upcoming NFL slate only")
    ap.add_argument("--season", type=int, default=model.inferred_season())
    ap.add_argument("--week", type=int, help="Optional NFL week; defaults to the next upcoming week")
    ap.add_argument("--schedule-csv", help="Use a local schedule CSV for testing/offline")
    ap.add_argument("--stats-csv", help="Use a local stats CSV for testing/offline")
    args = ap.parse_args(argv)

    schedule = load_schedule(args.schedule_csv)
    week, slate = upcoming_games(schedule, args.season, week=args.week)
    if not slate:
        raise RuntimeError("No upcoming regular-season games found for that season/week. Check the schedule date or --week.")
    print(f"\nNEXT UPCOMING NFL SLATE: SEASON {args.season}, WEEK {week} ({len(slate)} games remaining)")
    for g in slate:
        print(f"  {g['day']} {g['time_eastern']:>5} ET  {g['away']:>3} @ {g['home']:<3}")

    print(f"\nReading player statistics through week {week - 1}; no future-game stats used...")
    stats = model.nfl_stats(args.season, args.stats_csv)
    available_weeks = []
    for r in stats:
        if (r.get("season_type") or r.get("game_type") or "REG").upper() == "REG":
            try:
                available_weeks.append(int(float(r.get("week") or 0)))
            except ValueError:
                pass
    eligible = [w for w in available_weeks if w < week]
    if not eligible:
        raise RuntimeError(f"No completed player-stat weeks available before week {week}.")
    latest = max(eligible)
    if latest < week - 1:
        print(f"WARNING: stats only available through week {latest}; forecasts may be stale.")
    players = model.team_and_player_rates(stats, through_week=week - 1)
    active_team_games = {}
    for g in slate:
        active_team_games[g["home"]] = (g["away"], g)
        active_team_games[g["away"]] = (g["home"], g)
    latest_team = current_team_for_player(stats, week - 1)

    ranked = []
    for p in players:
        game_ref = active_team_games.get(p["team"])
        if not game_ref or latest_team.get(p["player_key"], p["team"]) != p["team"]:
            continue
        opponent, g = game_ref
        ranked.append({**p, "opponent": opponent,
                       "game": f"{g['away']} @ {g['home']}",
                       "week": week, "gameday": g["day"],
                       "gametime_eastern": g["time_eastern"],
                       "stats_through_week": latest})
    ranked.sort(key=lambda p: p["model_probability"], reverse=True)
    print(f"\nWEEK {week} PLAYER TD ESTIMATES — EXPERIMENTAL, NOT BACKTESTED\n")
    print(f"{'Player':<24} {'Team':<5} {'Opp':<5} {'Pos':<4} {'Model %':>8} {'Season Carries':>14} {'Season Tgt':>11}")
    for p in ranked[:30]:
        print(f"{p['player'][:24]:<24} {p['team']:<5} {p['opponent']:<5} {p['position']:<4} "
              f"{p['model_probability']:>7.1%} {p['season_carries']:>14} {p['season_targets']:>11}")
    OUTPUT.mkdir(exist_ok=True)
    target = OUTPUT / f"week_{week}_{args.season}_td_probabilities.csv"
    if ranked:
        fields = ["week", "gameday", "gametime_eastern", "game", "player", "team", "opponent", "position",
                  "model_probability", "expected_tds", "season_carries", "season_targets",
                  "rush_usage_share", "target_share", "player_games", "season_games_team", "stats_through_week"]
        with target.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(ranked)
        print(f"\nSaved {len(ranked)} scheduled players to: {target}")
    else:
        print("No modeled players match this slate. Check team codes/data availability.")
    print("NOTE: season carry/target columns are accumulated totals, not next-game forecasts.")
    print("This model does not yet account for injuries, lineup status, game totals, weather, or defenses.")
    print("Probability estimates have not been calibrated. For sports analytics research only.\n")


if __name__ == "__main__":
    try:
        run()
    except (RuntimeError, OSError, ValueError, csv.Error) as exc:
        print(f"\nERROR: {exc}")
        sys.exit(1)
