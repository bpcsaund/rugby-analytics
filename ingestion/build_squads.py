"""Build the squad-history table from announced-squad inputs.

Inputs: data/raw/squads/{team}_{window}.csv, one per team per announcement
(columns: announced, window, player, position_group, club, caps_stated, uncapped).
aliases.csv maps announced names to roster spellings (e.g. "Rob" -> "Robert").
manual_players.csv (team,player,dob YYYY-MM-DD,caps,source) supplies dob/caps for
players the WR roster lacks; roster entries win if both exist, unless the row's
source starts with `override`, which replaces the roster's dob/caps for that player.
positions.csv (from derive_positions.py) gives the second-level `position`
(squads.csv rows are ordered forwards then backs, by `position_order`: prop 1, hooker 2,
lock 3, back row 4, scrum-half 5, fly-half 6, centre 7, back three 8, hybrid 9, utility 10)
(prop/hooker/lock/back row/scrum-half/fly-half/centre/back three/hybrid).
squad_summary.csv is one row per team: the first announced final squad ("initial") against
the latest revision ("current") for size, uncapped, avg age/caps and distinct clubs, plus who
came in and went out overall. squad_changes has squad_type (training|final), revision (0 = as announced, then one per
change) and is_initial / is_latest flags (final squads only). A training squad is a
separate formal squad (England), reported in its own *_training columns.
clubs.csv (club,country) maps each club to its country; squads.csv gets club_country and
squad_changes gets players_at_home / players_abroad (club country vs the nation) and
club_countries. Add a line to clubs.csv for any new club (unmapped clubs are warned about).
squad_changes.distinct_clubs counts clubs among players with a club recorded
(clubs_known of size); only comparable between nations once clubs_known == size.
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

last_selected / years_since_selected: most recent matchday-23 (start or bench, from
{team}_ages.csv, Tests since 2015) before the announcement; blank = never selected in
that data (uncapped, or last capped before 2015). squad_changes buckets these.

Roster `caps` is a live career total (see build_player_roster.py), so
caps_roster is "latest known", not caps-at-announcement; caps_stated is the
figure printed in the announcement where the source gave one.

Usage: .venv/bin/python3 ingestion/build_squads.py
"""
import csv
import re
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


POSITION_ORDER = {"prop": 1, "hooker": 2, "lock": 3, "back row": 4, "scrum-half": 5, "fly-half": 6,
                  "centre": 7, "back three": 8, "hybrid": 9, "utility": 10}


def squad_type(window):
    """A `-training` window is a separate formal squad (e.g. England's camp group);
    everything else is the final squad."""
    return "training" if window.endswith("-training") else "final"


def revision(window):
    """0 = as first announced; `-rN` windows are the (N-1)th change to the same squad."""
    m = re.search(r"-r(\d+)$", window)
    return int(m.group(1)) - 1 if m else 0


def is_uncapped(v):
    """Uncapped if flagged in the announcement, stated as 0 caps, or 0 caps in the roster/manual data."""
    return (v["uncapped_announced"] == "yes" or v["caps_stated"] == "0"
            or v["caps_roster"] == "0")


def last_test_23(team):
    p = OUT_DIR / f"{team}_ages.csv"
    rows = read(p)
    last = max(d(r["date"]) for r in rows)
    return last, rows[0] and {norm(r["player"]) for r in rows if d(r["date"]) == last}


def main():
    squads, changes = [], []
    members = {}  # (team, window) -> set of player names
    aliases = read(IN_DIR / "aliases.csv")
    pos2 = {(r["team"], r["player"]): r for r in read(IN_DIR / "positions.csv")}
    manual = read(IN_DIR / "manual_players.csv")
    club_country = {r["club"]: r["country"] for r in read(IN_DIR / "clubs.csv")}
    files = sorted(p for p in IN_DIR.glob("*.csv") if p.name not in ("aliases.csv", "manual_players.csv", "positions.csv", "clubs.csv"))
    by_team = {}
    for p in files:
        team = p.stem.rsplit("_", 1)[0]
        by_team.setdefault(team, []).append(p)

    home = {t: t.replace("_", " ").title() for t in by_team}  # nation name as used in clubs.csv

    for team, paths in by_team.items():
        roster = {norm(r["player"]): r for r in read(OUT_DIR / f"{team}_roster.csv")}
        for m in manual:  # hand-supplied dob/caps for players the WR roster lacks
            if m["team"] == team:
                if m["source"].startswith("override"):  # user's figure beats the WR roster's
                    prev = roster.get(norm(m["player"]), {})
                    roster[norm(m["player"])] = {**prev, **{k: v for k, v in m.items() if v != ""}}
                else:
                    roster.setdefault(norm(m["player"]), m)
        alias = {norm(a["announced_name"]): norm(a["roster_name"]) for a in aliases if a["team"] == team}
        last_date, last23 = last_test_23(team)
        sel = {}  # norm(player) -> sorted matchday-23 dates (Tests since 2015)
        for r in read(OUT_DIR / f"{team}_ages.csv"):
            sel.setdefault(norm(r["player"]), []).append(d(r["date"]))
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
                prior = [x for x in sel.get(alias.get(k, k), []) if x < ann]
                last_sel = max(prior) if prior else None
                cur[k] = {
                    "team": team, "window": r["window"], "squad_type": squad_type(r["window"]),
                    "revision": revision(r["window"]), "announced": announced,
                    "player": r["player"], "position_group": r["position_group"],
                    "position": pos2.get((team, r["player"]), {}).get("position", ""),
                    "cover": pos2.get((team, r["player"]), {}).get("cover", ""),
                    "position_order": POSITION_ORDER.get(pos2.get((team, r["player"]), {}).get("position", ""), ""),
                    "club": r["club"], "club_country": club_country.get(r["club"], ""),
                    "caps_stated": r["caps_stated"],
                    "uncapped_announced": r["uncapped"], "in_roster": "yes" if ro else "no",
                    "dob": dob, "age_on_announcement": age,
                    "caps_roster": ro["caps"] if ro else "",
                    "in_last_test_23": "yes" if k in last23 else "no",
                    "last_test_date": last_date.strftime("%d/%m/%Y"),
                    "last_selected": last_sel.strftime("%d/%m/%Y") if last_sel else "",
                    "years_since_selected": round((ann - last_sel).days / 365.2425, 2) if last_sel else "",
                }
            squads.extend(cur.values())
            ages = [float(v["age_on_announcement"]) for v in cur.values() if v["age_on_announcement"] != ""]
            caps = [int(v["caps_roster"]) for v in cur.values() if v["caps_roster"] != ""]
            unmatched = sorted(v["player"] for v in cur.values() if v["in_roster"] == "no")
            ins = sorted(cur[k]["player"] for k in cur if prev is not None and k not in prev)
            outs = sorted(prev[k]["player"] for k in prev if k not in cur) if prev else []
            gaps = [float(v["years_since_selected"]) for v in cur.values() if v["years_since_selected"] != ""]
            clubs = {v["club"] for v in cur.values() if v["club"]}
            countries = [v["club_country"] for v in cur.values() if v["club_country"]]
            changes.append({
                "team": team, "window": rows[0]["window"], "squad_type": squad_type(rows[0]["window"]),
                "revision": revision(rows[0]["window"]), "announced": announced,
                "size": len(cur),
                "clubs_known": sum(1 for v in cur.values() if v["club"]),
                "distinct_clubs": len(clubs),
                "players_at_home": sum(c == home[team] for c in countries),
                "players_abroad": sum(c != home[team] for c in countries),
                "club_countries": len(set(countries)),
                "uncapped": sum(is_uncapped(v) for v in cur.values()),
                "sel_within_1y": sum(g < 1 for g in gaps),
                "sel_1_2y_ago": sum(1 <= g < 2 for g in gaps),
                "sel_2_3y_ago": sum(2 <= g < 3 for g in gaps),
                "sel_3y_plus_ago": sum(g >= 3 for g in gaps),
                "never_selected_since_2015": len(cur) - len(gaps),
                "uncapped_or_not_in_data": len(unmatched),
                "avg_age": round(sum(ages) / len(ages), 2) if ages else "",
                "avg_caps_roster": round(sum(caps) / len(caps), 1) if caps else "",
                "carried_from_last_test_23": sum(v["in_last_test_23"] == "yes" for v in cur.values()),
                "last_test_23_size": len(last23),
                "vs_previous_in": "; ".join(ins) if prev else "",
                "vs_previous_out": "; ".join(outs),
                "not_in_roster": "; ".join(unmatched),
            })
            members[(team, rows[0]["window"])] = {v["player"] for v in cur.values()}
            prev = cur

    def write(name, rows):
        with open(OUT_DIR / name, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        print(f"{name}: {len(rows)} rows")

    # forwards then backs; within each, shirt order (prop..back three), hybrid/utility last; stable otherwise
    block = {}
    for r in squads:
        block.setdefault((r["team"], r["window"]), len(block))
    squads.sort(key=lambda r: (block[(r["team"], r["window"])], r["position_group"] != "forwards",
                               r["position_order"] if r["position_order"] != "" else 99))
    unmapped = sorted({r["club"] for r in squads if r["club"] and not r["club_country"]})
    if unmapped:
        print("WARNING: clubs missing from clubs.csv:", "; ".join(unmapped))
    write("squads.csv", squads)
    METRICS = ("size", "uncapped", "avg_age", "avg_caps_roster", "distinct_clubs", "players_abroad", "club_countries")
    for team in dict.fromkeys(c["team"] for c in changes):
        fin = [c for c in changes if c["team"] == team and c["squad_type"] == "final"]
        for c in changes:
            if c["team"] == team:
                c["is_initial"] = c["is_latest"] = ""  # blank for training squads
        if fin:
            fin[0]["is_initial"], fin[-1]["is_latest"] = "yes", "yes"
            for c in fin[1:]:
                c["is_initial"] = "no"
            for c in fin[:-1]:
                c["is_latest"] = "no"
            if len(fin) == 1:
                fin[0]["is_initial"] = fin[0]["is_latest"] = "yes"
    write("squad_changes.csv", changes)

    summary = []
    for team in dict.fromkeys(c["team"] for c in changes):
        fin = [c for c in changes if c["team"] == team and c["squad_type"] == "final"]
        trn = [c for c in changes if c["team"] == team and c["squad_type"] == "training"]
        row = {"team": team, "final_squad": "named" if fin else "not named yet",
               "revisions": (fin[-1]["revision"] if fin else "")}
        if fin:
            a, b = fin[0], fin[-1]
            first, last = members[(team, a["window"])], members[(team, b["window"])]
            row.update(initial_window=a["window"], initial_announced=a["announced"],
                       current_window=b["window"], current_announced=b["announced"])
            for k in METRICS:
                row[f"{k}_initial"], row[f"{k}_current"] = a[k], b[k]
            row["players_in_vs_initial"] = "; ".join(sorted(last - first))
            row["players_out_vs_initial"] = "; ".join(sorted(first - last))
            row["net_changes"] = len(last - first) + len(first - last)
        else:
            for k in ("initial_window", "initial_announced", "current_window", "current_announced",
                      *[f"{m}_{x}" for m in METRICS for x in ("initial", "current")],
                      "players_in_vs_initial", "players_out_vs_initial", "net_changes"):
                row[k] = ""
        t = trn[-1] if trn else None
        row["training_window"] = t["window"] if t else ""
        row["training_announced"] = t["announced"] if t else ""
        for k in METRICS:
            row[f"{k}_training"] = t[k] if t else ""
        summary.append(row)
    write("squad_summary.csv", summary)
    for c in changes:
        print(f"  {c['team']:10s} {c['window']}: size {c['size']}, avg age {c['avg_age']}, "
              f"avg caps {c['avg_caps_roster']}, carried from last Test 23: "
              f"{c['carried_from_last_test_23']}/{c['last_test_23_size']}, "
              f"not in roster: {c['not_in_roster'] or '-'}")


if __name__ == "__main__":
    main()
