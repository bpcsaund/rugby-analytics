#!/usr/bin/env python3
"""
Fetch player biographical profiles from the incrowdsports API.

Reads all unique player IDs from data/raw/prem_player_stats.csv and calls:
  https://rugby-union-feeds.incrowdsports.com/v1/players/{id}?provider=rugbyviz&clientId=PRL

Outputs: data/raw/player_profiles.csv

Usage:
  python3 ingestion/player_profiles.py
"""

import csv
import json
import time
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
PLAYER_STATS_CSV = BASE_DIR / "data" / "raw" / "prem_player_stats.csv"
OUT_CSV = BASE_DIR / "data" / "raw" / "player_profiles.csv"

PROFILE_URL = (
    "https://rugby-union-feeds.incrowdsports.com/v1/players/{player_id}"
    "?provider=rugbyviz&clientId=PRL"
)

FIELDS = [
    "playerId", "firstName", "lastName",
    "dateOfBirth", "birthPlace",
    "height", "weight",
    "country",
    "nationalTeamId", "nationalTeamName",
    "teamId", "teamName",
]


def fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def val(v) -> str:
    return "" if v is None else str(v)


def dob_date(v) -> str:
    if not v:
        return ""
    return str(v)[:10]  # trim timestamp to YYYY-MM-DD


def collect_player_ids() -> tuple[list[str], dict[str, str]]:
    """Return ordered list of unique player IDs and a id→team name lookup."""
    ids: dict[str, None] = {}  # ordered set via dict
    team_lookup: dict[str, str] = {}
    with open(PLAYER_STATS_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            pid = row.get("player_id", "").strip()
            if pid:
                ids[pid] = None
                if pid not in team_lookup:
                    team_lookup[pid] = row.get("team", "")
    return list(ids), team_lookup


def fetch_profile(player_id: str, team_name: str) -> dict:
    url = PROFILE_URL.format(player_id=player_id)
    data = fetch_json(url)
    p = data["data"]
    # API returns teamId but no teamName; use the name from match stats CSV instead.
    return {
        "playerId":          val(p.get("playerId")),
        "firstName":         val(p.get("firstName")),
        "lastName":          val(p.get("lastName")),
        "dateOfBirth":       dob_date(p.get("dateOfBirth")),
        "birthPlace":        val(p.get("birthPlace")),
        "height":            val(p.get("height")),
        "weight":            val(p.get("weight")),
        "country":           val(p.get("country")),
        "nationalTeamId":    val(p.get("nationalTeamId")),
        "nationalTeamName":  val(p.get("nationalTeamName")),
        "teamId":            val(p.get("teamId")),
        "teamName":          team_name,
    }


def main() -> None:
    player_ids, team_lookup = collect_player_ids()
    print(f"Players to fetch: {len(player_ids)}")

    rows: list[dict] = []
    errors = 0

    for i, pid in enumerate(player_ids, 1):
        try:
            row = fetch_profile(pid, team_lookup.get(pid, ""))
            rows.append(row)
            print(f"  [{i}/{len(player_ids)}] {pid}  {row['firstName']} {row['lastName']}  ({row['country']})")
        except Exception as exc:
            print(f"  [{i}/{len(player_ids)}] ERROR {pid}: {exc}")
            errors += 1

        if i < len(player_ids):
            time.sleep(0.3)

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    print(f"\n✓ {len(rows)} profiles written | {errors} errors")
    print(f"  {OUT_CSV}")


if __name__ == "__main__":
    main()
