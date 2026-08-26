"""
Extract yellow/red card counts per team per match from the World Rugby match
timelines already cached under data/raw/wr_api_cache/match/ (fetched
originally for lineups/minutes -- see wr_national_teams_scraper.py). No new
network calls; this is a pure local extraction pass.

Output: data/raw/sheets_export/match_cards.csv, one row per team-perspective
per match: date, team, opposition, yellow_cards, red_cards. Card counts are
outcomes of the match itself (like tries or possession), not a pre-match
predictor on their own -- see build_intl_match_dataset.py's docstring on
leakage. A team's *rolling* discipline rate over recent matches, however, is
a legitimate pre-match feature, and is wired in there separately.
"""

import csv
import glob
import json
from pathlib import Path

import pandas as pd

from build_intl_match_dataset import normalize

CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "raw" / "wr_api_cache" / "match"
OUT_PATH = Path(__file__).resolve().parent.parent / "data" / "raw" / "sheets_export" / "match_cards.csv"


def main():
    summary_files = glob.glob(str(CACHE_DIR / "*_summary.json"))
    rows = []
    n_no_timeline, n_bad_date, n_ok = 0, 0, 0

    for sf in summary_files:
        alt_id = Path(sf).name.replace("_summary.json", "")
        tf = CACHE_DIR / f"{alt_id}_timeline.json"
        if not tf.exists():
            n_no_timeline += 1
            continue

        summary = json.load(open(sf))
        m = summary.get("match", {})
        teams = m.get("teams") or []
        if len(teams) != 2:
            continue
        date = pd.to_datetime((m.get("time") or {}).get("label"), errors="coerce")
        if pd.isna(date):
            n_bad_date += 1
            continue

        team_names = [normalize(t.get("name", "")) for t in teams]

        timeline = json.load(open(tf)).get("timeline") or []
        cards = {0: {"yellow": 0, "red": 0}, 1: {"yellow": 0, "red": 0}}
        for e in timeline:
            idx = e.get("teamIndex")
            if idx not in (0, 1):
                continue
            if e.get("type") == "Yellow":
                cards[idx]["yellow"] += 1
            elif e.get("type") == "Red":
                cards[idx]["red"] += 1

        date_str = date.strftime("%d/%m/%Y")
        rows.append(dict(date=date_str, team=team_names[0], opposition=team_names[1],
                          yellow_cards=cards[0]["yellow"], red_cards=cards[0]["red"]))
        rows.append(dict(date=date_str, team=team_names[1], opposition=team_names[0],
                          yellow_cards=cards[1]["yellow"], red_cards=cards[1]["red"]))
        n_ok += 1

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_PATH, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["date", "team", "opposition", "yellow_cards", "red_cards"])
        writer.writeheader()
        writer.writerows(rows)

    print(f"Extracted cards for {n_ok} matches ({len(rows)} team-perspective rows) -> {OUT_PATH}")
    print(f"Skipped: {n_no_timeline} missing timeline, {n_bad_date} unparseable date")
    total_yellow = sum(r["yellow_cards"] for r in rows)
    total_red = sum(r["red_cards"] for r in rows)
    print(f"Totals: {total_yellow} yellow-card team-incidents, {total_red} red-card team-incidents "
          f"(each match counted from both teams' perspective, so /2 for actual card count)")


if __name__ == "__main__":
    main()
