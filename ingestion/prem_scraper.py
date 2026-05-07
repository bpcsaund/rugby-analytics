#!/usr/bin/env python3
"""
incrowdsports API scraper — multi-competition.

Fetches match stats from the rugby-union-feeds.incrowdsports.com API and outputs:

  data/raw/{comp}_team_stats.csv
  data/raw/{comp}_player_stats.csv
  data/raw/{comp}_lineups.csv

Supported competitions (--competition flag):
  prem       Gallagher Premiership      (compId 1011) [default]
  top14      Top 14                     (compId 1002)
  champs     Investec Champions Cup     (compId 1008)
  challenge  European Challenge Cup     (compId 1026)
  urc        United Rugby Championship  (compId 1068)
  japan      Japan League One           (compId 2074)

Column names match rfu_scraper.py conventions where stats overlap.

Usage:
  python ingestion/prem_scraper.py                              # Prem, Bath only (test)
  python ingestion/prem_scraper.py --all-teams                  # Prem, all teams
  python ingestion/prem_scraper.py --competition urc --all-teams
  python ingestion/prem_scraper.py --competition champs --season 202401
  python ingestion/prem_scraper.py --append                     # append to existing CSVs
"""

import csv
import json
import sys
import time
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent.parent
OUT_DIR = BASE_DIR / "data" / "raw"

FIXTURES_URL = (
    "https://rugby-union-feeds.incrowdsports.com/v1/matches"
    "?provider=rugbyviz&season={season}&compId={comp_id}&sort=date"
)
# client_id_param is "&clientId=XXX" when the comp has a dedicated client, else ""
MATCH_URL = (
    "https://rugby-union-feeds.incrowdsports.com/v1/matches/{match_id}"
    "?provider=rugbyviz&seasonId={season}{client_id_param}"
)

COMPETITIONS: dict[str, dict] = {
    "prem":      {"comp_id": "1011", "label": "Gallagher Premiership",    "client_id": "PRL"},
    "top14":     {"comp_id": "1002", "label": "Top 14",                   "client_id": ""},
    "champs":    {"comp_id": "1008", "label": "Investec Champions Cup",   "client_id": ""},
    "challenge": {"comp_id": "1026", "label": "European Challenge Cup",   "client_id": ""},
    "urc":       {"comp_id": "1068", "label": "United Rugby Championship","client_id": ""},
    "japan":     {"comp_id": "2074", "label": "Japan League One",         "client_id": ""},
    "premcup":   {"comp_id": "1297", "label": "Premiership Rugby Cup",    "client_id": "PRL"},
}

DEFAULT_SEASONS = ["202401", "202501"]   # 202401 = 2024-25,  202501 = 2025-26
BATH_NAME = "Bath Rugby"

# ---------------------------------------------------------------------------
# Output schema  — mirrors rfu_scraper.py field names where stats overlap
# Extra Prem-only fields (territory_pct, Kick_Metres, round) are appended.
# ---------------------------------------------------------------------------
TEAM_FIELDS = [
    "match_url", "match_id", "competition", "season",
    "competition_name", "match_date", "round",
    "home_team", "away_team",
    "team", "home_away",
    "home_score_FT", "away_score_FT", "home_score_HT", "away_score_HT",
    "possession_pct", "territory_pct",
    # Attack
    "Tries", "Metres_Gained", "Carries", "Defenders_Beaten",
    "Clean_Breaks", "Passes", "Offloads", "Turnovers_Conceded",
    # Defence
    "Tackles_Attempted", "Missed_Tackles", "Turnovers_Won",
    # Kicking
    "Kicks_In_Play", "Kick_Metres",
    "Conversions", "Conversion_Accuracy",
    "Penalty_Goals", "Penalty_Goal_Accuracy",
    "Drop_Goals",
    # Breakdown
    "Rucks_Won", "Rucks_Lost", "Rucks_Won_Pct", "Mauls_Won",
    # Set Plays
    "Lineouts_Won", "Lineouts_Lost", "Lineouts_Won_Pct",
    "Scrums_Won", "Scrums_Lost", "Scrums_Won_Pct",
    # Discipline
    "Penalties_Conceded", "Red_Cards", "Yellow_Cards",
]

PLAYER_FIELDS = [
    "match_url", "match_id", "competition", "season",
    "match_date", "home_team", "away_team",
    "team", "home_away", "player_id", "player",
    "position", "position_id", "minutes_played",
    # Attack
    "Tries", "Metres_Gained", "Carries", "Defenders_Beaten",
    "Clean_Breaks", "Passes", "Offloads", "Turnovers_Conceded",
    "Try_Assists", "Points_Scored",
    # Defence
    "Tackles_Attempted", "Missed_Tackles", "Turnovers_Won",
    # Kicking
    "Kicks_In_Play", "Conversions", "Penalty_Goals", "Drop_Goals",
    # Set Plays
    "Lineouts_Won", "Lineout_Steals",
    # Discipline
    "Penalties_Conceded", "Red_Cards", "Yellow_Cards",
]

LINEUP_FIELDS = [
    "match_url", "match_id", "season", "team", "home_away",
    "shirt_number", "player_id", "player_name", "position", "role",
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def val(v) -> str:
    """Return API value as string; empty string for None."""
    return "" if v is None else str(v)


def pct_str(v) -> str:
    """Convert a 0–1 fraction to a rounded percentage string, e.g. '46.7'."""
    if v is None:
        return ""
    try:
        return str(round(float(v) * 100, 1))
    except (TypeError, ValueError):
        return ""


def accuracy_str(scored, missed) -> str:
    """Compute success-rate % from scored and missed counts."""
    try:
        total = int(scored or 0) + int(missed or 0)
        return "" if total == 0 else str(round(int(scored or 0) / total * 100, 1))
    except (TypeError, ValueError):
        return ""


def match_url(match_id: int, comp_key: str) -> str:
    if comp_key == "prem":
        return f"https://www.premiershiprugby.com/match-centre/{match_id}/team-stats"
    return f"https://rugby-union-feeds.incrowdsports.com/v1/matches/{match_id}"


# ---------------------------------------------------------------------------
# Fixture fetching
# ---------------------------------------------------------------------------
def get_fixtures(seasons: list[str], comp_id: str, bath_only: bool) -> list[dict]:
    """Return list of {match_id, season} dicts, optionally filtered to Bath."""
    results = []
    for season in seasons:
        data = fetch_json(FIXTURES_URL.format(season=season, comp_id=comp_id))
        for m in data.get("data", []):
            home = m.get("homeTeam", {}).get("name", "")
            away = m.get("awayTeam", {}).get("name", "")
            if bath_only and BATH_NAME not in (home, away):
                continue
            results.append({"match_id": m["id"], "season": season})
    return results


# ---------------------------------------------------------------------------
# Row parsers
# ---------------------------------------------------------------------------
def parse_team_row(m: dict, team_data: dict, home_away: str, comp_key: str = "prem") -> dict:
    s = team_data.get("stats") or {}
    row = {f: "" for f in TEAM_FIELDS}
    row.update({
        "match_url":          match_url(m["id"], comp_key),
        "match_id":           str(m["id"]),
        "competition":        str(m.get("compId", "")),
        "season":             str(m.get("season", "")),
        "competition_name":   m.get("compName", ""),
        "match_date":         (m.get("date") or "")[:10],
        "round":              str(m.get("round", "")),
        "home_team":          m["homeTeam"].get("name", ""),
        "away_team":          m["awayTeam"].get("name", ""),
        "team":               team_data.get("name", ""),
        "home_away":          home_away,
        "home_score_FT":      val(m["homeTeam"].get("score")),
        "away_score_FT":      val(m["awayTeam"].get("score")),
        "home_score_HT":      val(m["homeTeam"].get("halfTimeScore")),
        "away_score_HT":      val(m["awayTeam"].get("halfTimeScore")),
        "possession_pct":     pct_str(s.get("possession")),
        "territory_pct":      pct_str(s.get("territory")),
        # Attack
        "Tries":              val(s.get("tries")),
        "Metres_Gained":      val(s.get("metres")),
        "Carries":            val(s.get("carries")),
        "Defenders_Beaten":   val(s.get("defendersBeaten")),
        "Clean_Breaks":       val(s.get("cleanBreaks")),
        "Passes":             val(s.get("passes")),
        "Offloads":           val(s.get("offload")),
        "Turnovers_Conceded": val(s.get("turnoversConceded")),
        # Defence
        "Tackles_Attempted":  val(s.get("tackles")),
        "Missed_Tackles":     val(s.get("missedTackles")),
        "Turnovers_Won":      val(s.get("turnoversWon")),
        # Kicking
        "Kicks_In_Play":      val(s.get("kicksFromHand")),
        "Kick_Metres":        val(s.get("kickMetres")),
        "Conversions":        val(s.get("conversionGoals")),
        "Conversion_Accuracy": accuracy_str(s.get("conversionGoals"), s.get("missedConversionGoals")),
        "Penalty_Goals":      val(s.get("penaltyGoals")),
        "Penalty_Goal_Accuracy": accuracy_str(s.get("penaltyGoals"), s.get("missedPenaltyGoals")),
        "Drop_Goals":         val(s.get("dropGoalsConverted")),
        # Breakdown
        "Rucks_Won":          val(s.get("rucksWon")),
        "Rucks_Lost":         val(s.get("rucksLost")),
        "Rucks_Won_Pct":      pct_str(s.get("ruckSuccess")),
        "Mauls_Won":          val(s.get("maulsWon")),
        # Set Plays
        "Lineouts_Won":       val(s.get("lineoutsWon")),
        "Lineouts_Lost":      val(s.get("lineoutsLost")),
        "Lineouts_Won_Pct":   pct_str(s.get("lineoutSuccess")),
        "Scrums_Won":         val(s.get("scrumsWon")),
        "Scrums_Lost":        val(s.get("scrumsLost")),
        "Scrums_Won_Pct":     pct_str(s.get("scrumsSuccess")),
        # Discipline
        "Penalties_Conceded": val(s.get("penaltiesConceded")),
        "Red_Cards":          val(s.get("redCards")),
        "Yellow_Cards":       val(s.get("yellowCards")),
    })
    return row


def parse_player_rows(m: dict, team_data: dict, home_away: str, comp_key: str = "prem") -> list[dict]:
    rows = []
    for p in team_data.get("players") or []:
        s = p.get("stats") or {}
        pos_id = p.get("positionId") or 0
        rows.append({
            **{f: "" for f in PLAYER_FIELDS},
            "match_url":          match_url(m["id"], comp_key),
            "match_id":           str(m["id"]),
            "competition":        str(m.get("compId", "")),
            "season":             str(m.get("season", "")),
            "match_date":         (m.get("date") or "")[:10],
            "home_team":          m["homeTeam"].get("name", ""),
            "away_team":          m["awayTeam"].get("name", ""),
            "team":               team_data.get("name", ""),
            "home_away":          home_away,
            "player_id":          str(p.get("id", "")),
            "player":             p.get("known") or p.get("name", ""),
            "position":           p.get("position", "") or "",
            "position_id":        str(pos_id),
            "minutes_played":     val(s.get("minutesPlayedTotal")),
            # Attack
            "Tries":              val(s.get("tries")),
            "Metres_Gained":      val(s.get("metres")),
            "Carries":            val(s.get("carries")),
            "Defenders_Beaten":   val(s.get("defendersBeaten")),
            "Clean_Breaks":       val(s.get("cleanBreaks")),
            "Passes":             val(s.get("passes")),
            "Offloads":           val(s.get("offload")),
            "Turnovers_Conceded": val(s.get("turnoversConceded")),
            "Try_Assists":        val(s.get("tryAssists")),
            "Points_Scored":      val(s.get("points")),
            # Defence
            "Tackles_Attempted":  val(s.get("tackles")),
            "Missed_Tackles":     val(s.get("missedTackles")),
            "Turnovers_Won":      val(s.get("turnoverWon")),
            # Kicking
            "Kicks_In_Play":      val(s.get("kicksFromHand")),
            "Conversions":        val(s.get("conversionGoals")),
            "Penalty_Goals":      val(s.get("penaltyGoals")),
            "Drop_Goals":         val(s.get("dropGoalsConverted")),
            # Set Plays
            "Lineouts_Won":       val(s.get("lineoutsWon")),
            "Lineout_Steals":     val(s.get("lineoutSteals")),
            # Discipline
            "Penalties_Conceded": val(s.get("penaltiesConceded")),
            "Red_Cards":          val(s.get("redCards")),
            "Yellow_Cards":       val(s.get("yellowCards")),
        })
    return rows


def parse_lineup_rows(m: dict, team_data: dict, home_away: str, comp_key: str = "prem") -> list[dict]:
    rows = []
    for p in team_data.get("players") or []:
        pos_id = p.get("positionId") or 0
        rows.append({
            "match_url":   match_url(m["id"], comp_key),
            "match_id":    str(m["id"]),
            "season":      str(m.get("season", "")),
            "team":        team_data.get("name", ""),
            "home_away":   home_away,
            "shirt_number": str(pos_id),
            "player_id":   str(p.get("id", "")),
            "player_name": p.get("known") or p.get("name", ""),
            "position":    p.get("position", "") or "",
            "role":        "starter" if pos_id <= 15 else "replacement",
        })
    return rows


# ---------------------------------------------------------------------------
# CSV writer
# ---------------------------------------------------------------------------
def write_csv(path: Path, fieldnames: list[str], rows: list[dict], append: bool) -> None:
    mode = "a" if append else "w"
    with open(path, mode, newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if not append:
            w.writeheader()
        w.writerows(rows)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    args = sys.argv[1:]
    append    = "--append"    in args
    all_teams = "--all-teams" in args

    # --competition KEY
    comp_key = "prem"
    if "--competition" in args:
        idx = args.index("--competition")
        if idx + 1 < len(args):
            comp_key = args[idx + 1]
    if comp_key not in COMPETITIONS:
        print(f"ERROR: unknown competition '{comp_key}'. Choose from: {', '.join(COMPETITIONS)}")
        sys.exit(1)
    comp = COMPETITIONS[comp_key]

    # --season YYYYSS  (can be specified multiple times; default: both)
    seasons: list[str] = []
    for i, a in enumerate(args):
        if a == "--season" and i + 1 < len(args):
            seasons.append(args[i + 1])
    if not seasons:
        seasons = DEFAULT_SEASONS

    # Bath-only filter only applies to Prem without --all-teams
    bath_only = (comp_key == "prem") and not all_teams

    client_id_param = f"&clientId={comp['client_id']}" if comp["client_id"] else ""

    print(f"Competition : {comp['label']} (compId {comp['comp_id']})")
    print(f"Mode        : {'Bath Rugby only' if bath_only else 'all teams'}")
    print(f"Seasons     : {seasons}")
    print(f"Append      : {append}\n")

    fixtures = get_fixtures(seasons, comp["comp_id"], bath_only=bath_only)
    print(f"Matches to fetch: {len(fixtures)}\n")

    team_rows:   list[dict] = []
    player_rows: list[dict] = []
    lineup_rows: list[dict] = []
    errors = 0

    for i, fix in enumerate(fixtures, 1):
        mid    = fix["match_id"]
        season = fix["season"]
        try:
            url  = MATCH_URL.format(match_id=mid, season=season, client_id_param=client_id_param)
            data = fetch_json(url)
            m    = data["data"]
        except Exception as exc:
            print(f"  [{i}/{len(fixtures)}] ERROR match {mid}: {exc}")
            errors += 1
            continue

        ht     = m["homeTeam"]
        at     = m["awayTeam"]
        status = m.get("status", "fixture")

        print(
            f"  [{i}/{len(fixtures)}] {mid}  {m['date'][:10]}  "
            f"{ht['name']} {val(ht.get('score'))} - {val(at.get('score'))} {at['name']}"
            f"  [{status}]"
        )

        lineup_rows.extend(parse_lineup_rows(m, ht, "home", comp_key))
        lineup_rows.extend(parse_lineup_rows(m, at, "away", comp_key))

        if status == "result":
            team_rows.append(parse_team_row(m, ht, "home", comp_key))
            team_rows.append(parse_team_row(m, at, "away", comp_key))
            player_rows.extend(parse_player_rows(m, ht, "home", comp_key))
            player_rows.extend(parse_player_rows(m, at, "away", comp_key))

        if i < len(fixtures):
            time.sleep(0.3)  # polite rate limiting

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    team_csv   = OUT_DIR / f"{comp_key}_team_stats.csv"
    player_csv = OUT_DIR / f"{comp_key}_player_stats.csv"
    lineup_csv = OUT_DIR / f"{comp_key}_lineups.csv"

    write_csv(team_csv,   TEAM_FIELDS,   team_rows,   append)
    write_csv(player_csv, PLAYER_FIELDS, player_rows, append)
    write_csv(lineup_csv, LINEUP_FIELDS, lineup_rows, append)

    result_count = len(team_rows) // 2
    print(f"\n✓ {result_count} completed matches | {errors} errors")
    print(f"  {team_csv}   ({len(team_rows)} team rows)")
    print(f"  {player_csv} ({len(player_rows)} player rows)")
    print(f"  {lineup_csv} ({len(lineup_rows)} lineup rows)")


if __name__ == "__main__":
    main()
