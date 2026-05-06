#!/usr/bin/env python3
"""
Probe incrowdsports API for rugby competitions beyond Premiership (compId=1011).

Tries known and guessed compId values, fetching the fixtures list for a recent
season and reporting what competitions appear in the response.

Usage:
  python ingestion/explore_comps.py
"""

import json
import time
import urllib.request

SEASONS = ["202401", "202501"]

# Known: 1011 = Premiership
# Candidates based on common numbering patterns and known provider info
COMP_IDS = [
    # Confirmed
    1011,   # English Premiership
    # European / tier-1 candidates
    1,      # often used for EPCR Champions Cup
    2,      # often used for EPCR Challenge Cup
    5,      # possible URC / Pro14
    10,     # possible URC
    21,
    100,
    101,
    102,
    103,
    104,
    105,
    106,
    107,
    108,
    109,
    110,
    1000,
    1001,
    1002,
    1003,
    1004,
    1005,
    1006,
    1007,
    1008,
    1009,
    1010,
    1012,
    1013,
    1014,
    1015,
    1016,
    1017,
    1018,
    1019,
    1020,
    1021,
    1022,
    1023,
    1024,
    1025,
    1030,
    1040,
    1050,
    1100,
    1200,
    2000,
    2001,
    2011,
    3000,
    4000,
    5000,
]

BASE_URL = (
    "https://rugby-union-feeds.incrowdsports.com/v1/matches"
    "?provider=rugbyviz&season={season}&compId={comp_id}&sort=date"
)


def fetch(url: str) -> dict | None:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except Exception as exc:
        return {"_error": str(exc)}


def main() -> None:
    hits = []
    print(f"{'compId':<8} {'season':<8} {'matches':<8} {'compName'}")
    print("-" * 60)
    for comp_id in COMP_IDS:
        for season in SEASONS:
            url = BASE_URL.format(season=season, comp_id=comp_id)
            data = fetch(url)
            if "_error" in data:
                continue
            matches = data.get("data", [])
            if not matches:
                continue
            # Pull competition name from first match
            first = matches[0]
            comp_name = first.get("compName", "")
            print(f"{comp_id:<8} {season:<8} {len(matches):<8} {comp_name}")
            hits.append({
                "comp_id": comp_id,
                "season": season,
                "n_matches": len(matches),
                "comp_name": comp_name,
                "teams": sorted({
                    m.get("homeTeam", {}).get("name", "")
                    for m in matches
                } | {
                    m.get("awayTeam", {}).get("name", "")
                    for m in matches
                } - {""})[:6],
            })
            time.sleep(0.2)
        time.sleep(0.1)

    print("\n=== Summary of hits ===")
    seen = set()
    for h in hits:
        key = (h["comp_id"], h["comp_name"])
        if key in seen:
            continue
        seen.add(key)
        print(f"  compId={h['comp_id']:>6}  {h['comp_name']}")
        print(f"           sample teams: {', '.join(h['teams'][:4])}")

    if not hits:
        print("No competitions found beyond known ones.")


if __name__ == "__main__":
    main()
