"""Given a newly-announced squad (list of player names) for one of the 10
tracked national teams, look up each player's age-as-of-matchday and current
caps from the {team}_roster.csv built by build_player_roster.py.

Usage:
    python3 ingestion/squad_lookup.py --team wales --date 08/11/2026 --squad squad.txt

squad.txt: one player per line, optionally "shirt,name" (e.g. "1,Rhys Carre"
or just "Rhys Carre" - shirt numbers are cosmetic/output-only, matching is
by name). Blank lines and lines starting with # are ignored.

Name matching: exact match against the roster first, then an accent/case/
punctuation-insensitive match, then (if still nothing) prints the closest
roster names so you can fix a typo or confirm it's a genuine uncapped
debutant not yet in our data.

See build_player_roster.py's docstring for why `caps` is a live total, not
historical-at-that-match - this script inherits that same caveat.
"""
import argparse
import csv
import difflib
import sys
import unicodedata
from datetime import date, datetime
from pathlib import Path

ROOT = Path.home() / "rugby_analytics"
OUT_DIR = ROOT / "data/raw/sheets_export"


def normalize(name: str) -> str:
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return "".join(c.lower() for c in n if c.isalnum())


def load_roster(team: str) -> dict:
    roster_file = OUT_DIR / f"{team}_roster.csv"
    if not roster_file.exists():
        sys.exit(f"No roster file for team '{team}' at {roster_file}. "
                  f"Run build_player_roster.py first, or check the team key.")
    roster = {}
    by_norm = {}
    with open(roster_file, newline="") as f:
        for row in csv.DictReader(f):
            roster[row["player"]] = row
            by_norm[normalize(row["player"])] = row
    return roster, by_norm


def parse_squad_file(path: Path) -> list[tuple[str, str]]:
    entries = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "," in line:
            shirt, name = line.split(",", 1)
            entries.append((shirt.strip(), name.strip()))
        else:
            entries.append(("", line))
    return entries


def age_on(match_date: date, dob_str: str) -> float:
    dob = datetime.strptime(dob_str, "%Y-%m-%d").date()
    return round((match_date - dob).days / 365.2425, 2)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--team", required=True, help="e.g. wales, england, new_zealand")
    ap.add_argument("--date", required=True, help="match date, DD/MM/YYYY")
    ap.add_argument("--squad", required=True, type=Path, help="path to squad list file")
    ap.add_argument("--out", type=Path, help="optional CSV output path")
    args = ap.parse_args()

    match_date = datetime.strptime(args.date, "%d/%m/%Y").date()
    roster, by_norm = load_roster(args.team)
    entries = parse_squad_file(args.squad)

    results = []
    misses = []
    for shirt, name in entries:
        rec = roster.get(name) or by_norm.get(normalize(name))
        if rec is None:
            suggestions = difflib.get_close_matches(name, roster.keys(), n=3, cutoff=0.6)
            misses.append((name, suggestions))
            continue
        age = age_on(match_date, rec["dob"]) if rec["dob"] else None
        results.append({
            "shirt": shirt or rec.get("shirt_last_seen", ""),
            "player": rec["player"],
            "age": age if age is not None else "",
            "caps": rec["caps"],
            "dob": rec["dob"],
        })

    print(f"\n{args.team} squad for {args.date}\n")
    print(f"{'#':>3}  {'Player':25s} {'Age':>6}  {'Caps':>5}")
    def shirt_key(r):
        s = str(r["shirt"])
        return (s == "", int(s) if s.isdigit() else 999, s)

    for r in sorted(results, key=shirt_key):
        print(f"{str(r['shirt']):>3}  {r['player']:25s} {str(r['age']):>6}  {str(r['caps']):>5}")

    ages = [r["age"] for r in results if r["age"] != ""]
    caps = [int(r["caps"]) for r in results if str(r["caps"]).isdigit()]
    if ages:
        print(f"\nAverage age: {sum(ages)/len(ages):.2f}  (n={len(ages)})")
    if caps:
        print(f"Average caps: {sum(caps)/len(caps):.1f}  (n={len(caps)})")
        print(f"Total caps in room: {sum(caps)}")

    if misses:
        print(f"\n{len(misses)} name(s) not found in roster:")
        for name, suggestions in misses:
            hint = f" - did you mean: {', '.join(suggestions)}?" if suggestions else " - not in our data (uncapped debutant?)"
            print(f"  '{name}'{hint}")

    if args.out:
        with open(args.out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["shirt", "player", "age", "caps", "dob"])
            w.writeheader()
            w.writerows(results)
        print(f"\nWritten to {args.out}")


if __name__ == "__main__":
    main()
