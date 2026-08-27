"""
Extract venue (name, city, country) and exact kickoff time from the World
Rugby match summaries already cached under data/raw/wr_api_cache/match/ --
same source and no-new-network-calls pattern as build_match_cards.py.

Output: data/raw/sheets_export/match_venues.csv, one row per team-perspective
per match: date, team, opposition, venue, city, country, kickoff_utc (ISO),
gmt_offset (hours). Local kickoff time = kickoff_utc + gmt_offset.
"""

import csv
import glob
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from build_intl_match_dataset import normalize

CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "raw" / "wr_api_cache" / "match"
OUT_PATH = Path(__file__).resolve().parent.parent / "data" / "raw" / "sheets_export" / "match_venues.csv"


def main():
    summary_files = glob.glob(str(CACHE_DIR / "*_summary.json"))
    rows = []
    n_no_venue, n_bad_date = 0, 0

    for sf in summary_files:
        data = json.load(open(sf))
        m = data.get("match", {})
        teams = m.get("teams") or []
        if len(teams) != 2:
            continue
        venue = m.get("venue") or {}
        if not venue.get("name"):
            n_no_venue += 1
            continue

        date = pd.to_datetime((m.get("time") or {}).get("label"), errors="coerce")
        if pd.isna(date):
            n_bad_date += 1
            continue
        date_str = date.strftime("%d/%m/%Y")

        t = m.get("time") or {}
        millis = t.get("millis")
        gmt_offset = t.get("gmtOffset")
        kickoff_utc = (
            datetime.fromtimestamp(millis / 1000, tz=timezone.utc).isoformat()
            if millis else ""
        )

        team_names = [normalize(tm.get("name", "")) for tm in teams]
        venue_name = venue.get("name", "")
        city = (venue.get("city") or "").split("|")[0].strip()
        country = venue.get("country", "")

        for i in (0, 1):
            rows.append(dict(
                date=date_str, team=team_names[i], opposition=team_names[1 - i],
                venue=venue_name, city=city, country=country,
                kickoff_utc=kickoff_utc, gmt_offset=gmt_offset,
            ))

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_PATH, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "date", "team", "opposition", "venue", "city", "country", "kickoff_utc", "gmt_offset"
        ])
        writer.writeheader()
        writer.writerows(rows)

    print(f"Extracted venue/kickoff for {len(rows)//2} matches ({len(rows)} team-perspective rows) -> {OUT_PATH}")
    print(f"Skipped: {n_no_venue} missing venue, {n_bad_date} unparseable date")
    print(f"Unique venues: {len(set((r['venue'], r['city'], r['country']) for r in rows))}")


if __name__ == "__main__":
    main()
