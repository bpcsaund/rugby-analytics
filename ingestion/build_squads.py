"""Build the squad-history table from announced-squad inputs.

Inputs: data/raw/squads/{team}_{window}.csv, one per team per announcement
(columns: announced, window, player, position_group, club, caps_stated, uncapped).
aliases.csv maps announced names to roster spellings (e.g. "Rob" -> "Robert").
These are the source of truth - add a file whenever a squad is named
(or re-named after withdrawals/call-ups: use a new `window` suffix, e.g.
`2026-11-nations-championship-r2`, so the change is tracked).

Outputs (data/raw/sheets_export/):
  squads.csv         one row per player per announced squad, joined to
                     {team}_roster.csv (dob, live caps, age on announcement)
                     and flagged with whether they were in the team's most
                     recent matchday 23 (from {team}_ages.csv).
  squad_changes.csv  one row per team per window: size, uncapped, avg age,
                     avg caps, carried-over-from-last-Test-23 count, and
                     in/out vs the previous announced squad (if any).

Roster `caps` is a live career total (see build_player_roster.py), so
caps_roster is "latest known", not caps-at-announcement; caps_stated is the
figure printed in the announcement where the source gave one.

Usage: .venv/bin/python3 ingestion/build_squads.py
"""
import csv
import unicodedata
from datetime import datetime
from pathlib import Path

ROOT = Path.home() / "rugby_analytics"
IN_DIR = ROOT / "data/raw/squads"
OUT_DIR = ROOT / "data/raw/sheets_export"


def norm(s):
    n = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return "".join(c.lower() for c in n if c.isalnum())


def d(s, fmt="%d/%m/%Y"):
    return datetime.strptime(s, fmt).date()


def read(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def last_test_23(team):
    p = OUT_DIR / f"{team}_ages.csv"
    rows = read(p)
    last = max(d(r["date"]) for r in rows)
    return last, rows[0] and {norm(r["player"]) for r in rows if d(r["date"]) == last}


def main():
    squads, changes = [], []
    aliases = read(IN_DIR / "aliases.csv")
    files = sorted(p for p in IN_DIR.glob("*.csv") if p.name != "aliases.csv")
    by_team = {}
    for p in files:
        team = p.name.split("_", 1)[0]
        by_team.setdefault(team, []).append(p)

    for team, paths in by_team.items():
        roster = {norm(r["player"]): r for r in read(OUT_DIR / f"{team}_roster.csv")}
        alias = {norm(a["announced_name"]): norm(a["roster_name"]) for a in aliases if a["team"] == team}
        last_date, last23 = last_test_23(team)
        windows = sorted(((read(p)[0]["announced"], p) for p in paths), key=lambda x: d(x[0]))
        prev = None
        for announced, p in windows:
            rows = read(p)
            ann = d(announced)
            cur = {}
            for r in rows:
                k = norm(r["player"])
                ro = roster.get(alias.get(k, k))
                dob = ro["dob"] if ro else ""
                age = round((ann - datetime.strptime(dob, "%Y-%m-%d").date()).days / 365.2425, 2) if dob else ""
                cur[k] = {
                    "team": team, "window": r["window"], "announced": announced,
                    "player": r["player"], "position_group": r["position_group"],
                    "club": r["club"], "caps_stated": r["caps_stated"],
                    "uncapped_announced": r["uncapped"], "in_roster": "yes" if ro else "no",
                    "dob": dob, "age_on_announcement": age,
                    "caps_roster": ro["caps"] if ro else "",
                    "in_last_test_23": "yes" if k in last23 else "no",
                    "last_test_date": last_date.strftime("%d/%m/%Y"),
                }
            squads.extend(cur.values())
            ages = [float(v["age_on_announcement"]) for v in cur.values() if v["age_on_announcement"] != ""]
            caps = [int(v["caps_roster"]) for v in cur.values() if v["caps_roster"] != ""]
            unmatched = sorted(v["player"] for v in cur.values() if v["in_roster"] == "no")
            ins = sorted(cur[k]["player"] for k in cur if prev is not None and k not in prev)
            outs = sorted(prev[k]["player"] for k in prev if k not in cur) if prev else []
            changes.append({
                "team": team, "window": rows[0]["window"], "announced": announced,
                "size": len(cur),
                "uncapped_or_not_in_data": len(unmatched),
                "avg_age": round(sum(ages) / len(ages), 2) if ages else "",
                "avg_caps_roster": round(sum(caps) / len(caps), 1) if caps else "",
                "carried_from_last_test_23": sum(v["in_last_test_23"] == "yes" for v in cur.values()),
                "last_test_23_size": len(last23),
                "vs_previous_in": "; ".join(ins) if prev else "",
                "vs_previous_out": "; ".join(outs),
                "not_in_roster": "; ".join(unmatched),
            })
            prev = cur

    def write(name, rows):
        with open(OUT_DIR / name, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        print(f"{name}: {len(rows)} rows")

    write("squads.csv", squads)
    write("squad_changes.csv", changes)
    for c in changes:
        print(f"  {c['team']:10s} {c['window']}: size {c['size']}, avg age {c['avg_age']}, "
              f"avg caps {c['avg_caps_roster']}, carried from last Test 23: "
              f"{c['carried_from_last_test_23']}/{c['last_test_23_size']}, "
              f"not in roster: {c['not_in_roster'] or '-'}")


if __name__ == "__main__":
    main()
