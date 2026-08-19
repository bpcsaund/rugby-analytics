"""Build per-player matchday-age tables (and per-match squad-age summaries)
for all 10 tracked national teams, using the World Rugby pulselive API.

Source of truth for match list: the cached bulk match list under
data/raw/wr_api_cache/bulk/*.json (2019-01-01 to 2026-12-31, sport=mru),
already fetched while building the 6-team dataset. Per-match summaries
(which include each player's dob) are cached under
data/raw/wr_api_cache/match/{matchAltId}_summary.json — reused where
present (the 6-team build already populated ~half of these via shared
fixtures), fetched fresh otherwise. No timeline calls needed here (age
doesn't depend on substitution data).

Outputs:
  data/raw/sheets_export/{team}_ages.csv        - one row per player per match
  data/raw/sheets_export/team_age_summary.csv   - one row per team per match,
                                                   average age of starting XV / bench / full squad
"""
import csv
import glob
import json
import time
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path.home() / "rugby_analytics"
BULK_DIR = ROOT / "data/raw/wr_api_cache/bulk"
MATCH_DIR = ROOT / "data/raw/wr_api_cache/match"
OUT_DIR = ROOT / "data/raw/sheets_export"
MATCH_DIR.mkdir(parents=True, exist_ok=True)

TEAM_IDS = {
    "wales": "33", "england": "34", "scotland": "35", "ireland": "36",
    "new_zealand": "37", "australia": "38", "south_africa": "39",
    "argentina": "40", "italy": "41", "france": "42",
}


def fetch_summary(match_alt_id: str) -> dict:
    cache_file = MATCH_DIR / f"{match_alt_id}_summary.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text())
    url = f"https://api.wr-rims-prod.pulselive.com/rugby/v3/match/{match_alt_id}/summary"
    with urllib.request.urlopen(url) as resp:
        data = json.loads(resp.read())
    cache_file.write_text(json.dumps(data))
    time.sleep(0.25)
    return data


def matches_for_team(team_id: str) -> list[dict]:
    seen = {}
    for f in glob.glob(str(BULK_DIR / "*.json")):
        d = json.loads(Path(f).read_text())
        for m in d.get("content", []):
            if m.get("status") != "C":
                continue
            team_ids = [t["id"] for t in m.get("teams", [])]
            if team_id in team_ids:
                seen[m["matchAltId"]] = m
    return list(seen.values())


def age_on(match_date: date, dob_millis: int) -> float:
    dob = date.fromtimestamp(dob_millis / 1000)
    days = (match_date - dob).days
    return days / 365.2425


def main():
    team_summary_rows = []
    for team_key, team_id in TEAM_IDS.items():
        matches = matches_for_team(team_id)
        player_rows = []
        skipped = 0
        for m in matches:
            try:
                summary = fetch_summary(m["matchAltId"])
            except Exception as e:
                print(f"  skip {team_key} {m['matchAltId']}: fetch error {e}")
                skipped += 1
                continue
            match_teams = summary["match"]["teams"]
            team_idx = next((i for i, t in enumerate(match_teams) if t["id"] == team_id), None)
            if team_idx is None:
                skipped += 1
                continue
            opp = match_teams[1 - team_idx]["name"]
            match_date = date.fromisoformat(summary["match"]["time"]["label"])
            competition = summary["match"].get("competition", "")
            team_list = summary["teams"][team_idx]["teamList"]["list"]

            ages_starting, ages_bench, ages_all = [], [], []
            for entry in team_list:
                p = entry["player"]
                num = entry.get("number")
                if num in (None, ""):
                    continue  # unused squad member
                num = int(num)
                dob = p.get("dob")
                age_years = None
                if dob and dob.get("millis"):
                    age_years = round(age_on(match_date, dob["millis"]), 2)
                    ages_all.append(age_years)
                    (ages_starting if num <= 15 else ages_bench).append(age_years)
                player_rows.append({
                    "date": match_date.strftime("%d/%m/%Y"),
                    "opposition": opp,
                    "shirt": num,
                    "player": p["name"]["display"],
                    "dob": dob["label"] if dob else "",
                    "age_on_matchday": age_years if age_years is not None else "",
                })
            if ages_all:
                team_summary_rows.append({
                    "team": team_key,
                    "date": match_date.strftime("%d/%m/%Y"),
                    "opposition": opp,
                    "competition": competition,
                    "starting_xv_avg_age": round(sum(ages_starting) / len(ages_starting), 2) if ages_starting else "",
                    "bench_avg_age": round(sum(ages_bench) / len(ages_bench), 2) if ages_bench else "",
                    "full_squad_avg_age": round(sum(ages_all) / len(ages_all), 2),
                    "players_with_dob": len(ages_all),
                    "players_total": sum(1 for e in team_list if e.get("number") not in (None, "")),
                })

        player_rows.sort(key=lambda r: tuple(reversed(r["date"].split("/"))))
        out_file = OUT_DIR / f"{team_key}_ages.csv"
        with open(out_file, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["date", "opposition", "shirt", "player", "dob", "age_on_matchday"])
            w.writeheader()
            w.writerows(player_rows)
        print(f"{team_key}: {len(matches)} matches, {len(player_rows)} player-rows written, {skipped} skipped -> {out_file.name}")

    team_summary_rows.sort(key=lambda r: (r["team"], tuple(reversed(r["date"].split("/")))))
    summary_file = OUT_DIR / "team_age_summary.csv"
    with open(summary_file, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["team", "date", "opposition", "competition",
                                           "starting_xv_avg_age", "bench_avg_age", "full_squad_avg_age",
                                           "players_with_dob", "players_total"])
        w.writeheader()
        w.writerows(team_summary_rows)
    print(f"team_age_summary.csv: {len(team_summary_rows)} rows")


if __name__ == "__main__":
    main()
