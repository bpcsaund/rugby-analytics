"""Extend full-history minutes-played coverage (currently only Wales/Scotland/
Italy/New Zealand/South Africa/Argentina, from the original 6-team build) to
Ireland, England, and France, needed to compare Farrell/Borthwick/Galthié
workload usage against Erasmus/Townsend on equal footing.

Fetches missing timeline data (summaries are already cached from
build_squad_ages.py) and reuses the exact compute_minutes() algorithm from
wr_national_teams_scraper.py (including its IR/HIAO toggle-inference logic)
rather than reimplementing it.
"""
import csv
import glob
import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path.home() / "rugby_analytics"
sys.path.insert(0, str(ROOT / "ingestion"))
from wr_national_teams_scraper import compute_minutes  # noqa: E402

BULK_DIR = ROOT / "data/raw/wr_api_cache/bulk"
MATCH_DIR = ROOT / "data/raw/wr_api_cache/match"
OUT_DIR = ROOT / "data/raw/sheets_export"

TEAMS = {"ireland": "36", "england": "34", "france": "42"}


def fetch_timeline(match_alt_id: str) -> dict:
    cache_file = MATCH_DIR / f"{match_alt_id}_timeline.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text())
    url = f"https://api.wr-rims-prod.pulselive.com/rugby/v3/match/{match_alt_id}/timeline"
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
            if team_id in [t["id"] for t in m.get("teams", [])]:
                seen[m["matchAltId"]] = m
    return list(seen.values())


def main():
    for team_key, team_id in TEAMS.items():
        matches = matches_for_team(team_id)
        rows = []
        fetched = 0
        for m in matches:
            summ_file = MATCH_DIR / f"{m['matchAltId']}_summary.json"
            if not summ_file.exists():
                print(f"  {team_key} {m['matchAltId']}: no cached summary, skipping")
                continue
            summary = json.loads(summ_file.read_text())
            try:
                timeline = fetch_timeline(m["matchAltId"])
                fetched += 1
            except Exception as e:
                print(f"  {team_key} {m['matchAltId']}: timeline fetch failed: {e}")
                continue
            match_teams = summary["match"]["teams"]
            team_idx = next((i for i, t in enumerate(match_teams) if t["id"] == team_id), None)
            if team_idx is None:
                continue
            opp = match_teams[1 - team_idx]["name"]
            match_date = summary["match"]["time"]["label"]
            date_fmt = "/".join(reversed(match_date.split("-")))
            info = compute_minutes(team_idx, summary, timeline)
            for rec in info.values():
                rows.append({
                    "date": date_fmt,
                    "opposition": opp,
                    "shirt": rec["shirt"],
                    "player": rec["display"],
                    "started": "y" if rec["started"] else "n",
                    "minutes": rec["minutes"],
                })
        rows.sort(key=lambda r: tuple(reversed(r["date"].split("/"))))
        out_file = OUT_DIR / f"{team_key}_minutes.csv"
        with open(out_file, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["date", "opposition", "shirt", "player", "started", "minutes"])
            w.writeheader()
            w.writerows(rows)
        print(f"{team_key}: {len(matches)} matches, {fetched} timelines fetched, {len(rows)} rows -> {out_file.name}")


if __name__ == "__main__":
    main()
