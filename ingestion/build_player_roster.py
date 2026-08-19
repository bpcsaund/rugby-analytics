"""Build a per-team player roster: one row per player, taken from their most
recent cached match appearance, with dob and caps.

IMPORTANT: the World Rugby API's `caps` field is a live running career total,
not a snapshot of caps-as-of-that-match (verified: Alun Wyn Jones shows the
same caps figure across 42 matches spanning 2019-2023, despite playing
throughout). So this roster is only meaningful as a "latest known" reference
for each player (accurate as of their most recent appearance in our cached
data, which runs through 18/07/2026) - not for reconstructing historical
caps totals at an earlier match date. Don't merge this caps figure into the
historical {team}_ages.csv files, it would misrepresent the past.

Use case this supports: given a newly-announced squad (list of player
names), look up each player's dob here to compute their age as of the new
match date, and take their caps figure as a current best-estimate.
"""
import csv
import json
from pathlib import Path

ROOT = Path.home() / "rugby_analytics"
MATCH_DIR = ROOT / "data/raw/wr_api_cache/match"
OUT_DIR = ROOT / "data/raw/sheets_export"

TEAM_IDS = {
    "wales": "33", "england": "34", "scotland": "35", "ireland": "36",
    "new_zealand": "37", "australia": "38", "south_africa": "39",
    "argentina": "40", "italy": "41", "france": "42",
}


def main():
    # player_id -> {team_key: {best record}}
    rosters = {k: {} for k in TEAM_IDS}

    summary_files = list(MATCH_DIR.glob("*_summary.json"))
    for f in summary_files:
        d = json.loads(f.read_text())
        match_date = d["match"]["time"]["label"]
        match_teams = d["match"]["teams"]
        for idx, team in enumerate(match_teams):
            team_key = next((k for k, v in TEAM_IDS.items() if v == team["id"]), None)
            if team_key is None:
                continue
            for entry in d["teams"][idx]["teamList"]["list"]:
                p = entry["player"]
                num = entry.get("number")
                if num in (None, ""):
                    continue
                pid = p["id"]
                existing = rosters[team_key].get(pid)
                if existing is None or match_date > existing["last_match_date"]:
                    dob = p.get("dob")
                    rosters[team_key][pid] = {
                        "player": p["name"]["display"],
                        "dob": dob["label"] if dob else "",
                        "caps": p.get("caps", ""),
                        "position_last_seen": entry.get("positionLabel", ""),
                        "shirt_last_seen": num,
                        "last_match_date": match_date,
                        "last_opposition": match_teams[1 - idx]["name"],
                    }

    for team_key, players in rosters.items():
        rows = sorted(players.values(), key=lambda r: r["player"])
        out_file = OUT_DIR / f"{team_key}_roster.csv"
        with open(out_file, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=[
                "player", "dob", "caps", "position_last_seen", "shirt_last_seen",
                "last_match_date", "last_opposition",
            ])
            w.writeheader()
            for r in rows:
                r = dict(r)
                r["last_match_date"] = "/".join(reversed(r["last_match_date"].split("-")))
                w.writerow(r)
        print(f"{team_key}: {len(rows)} players -> {out_file.name}")


if __name__ == "__main__":
    main()
