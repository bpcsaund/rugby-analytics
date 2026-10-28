"""Derive a second-level position for every announced-squad player.

Writes data/raw/squads/positions.csv (team, player, position, basis), read by
build_squads.py to add a `position` column beside the forwards/backs
`position_group`. Positions: prop, hooker, lock, back row, scrum-half,
fly-half, centre, back three, hybrid, utility.

Evidence, in order: starts (shirt 1-15) since 2023-01-01; else starts since 2015;
else bench shirts 16-18 (hooker, props); 19-23 are ignored because a 6-2 bench
split makes 19-21 forwards of any kind and 22/23 cover any back; else the announcement's own label
(SOURCE_LABELS, SA/NZ only); else blank for a human to fill.

side is >=30% of starts and >=3 starts); versatility within one side keeps the
most-started position.
Rows with basis `manual` in the existing positions.csv are preserved, so hand
corrections survive a re-run. Positions follow the squad's players, so a new
squad window only needs this re-run.

`utility` (hand-set only) = plays 3 or more positions on the same side of the
forwards/backs divide (e.g. centre/fly-half/full-back); `cover` lists the others.
Covering just one other position keeps the main position, with the other in `cover`.
`cover` is a hand-filled note for within-side versatility (e.g. a centre who
can cover wing); manual rows keep it.

Usage: .venv/bin/python3 ingestion/derive_positions.py
"""
import collections
import csv
import unicodedata
from datetime import datetime
from pathlib import Path

ROOT = Path.home() / "rugby_analytics"
EXPORT = ROOT / "data/raw/sheets_export"
OUT = ROOT / "data/raw/squads/positions.csv"
RECENT = datetime(2023, 1, 1).date()

START = {1: "prop", 3: "prop", 2: "hooker", 4: "lock", 5: "lock", 6: "back row", 7: "back row",
         8: "back row", 9: "scrum-half", 10: "fly-half", 12: "centre", 13: "centre",
         11: "back three", 14: "back three", 15: "back three"}
BENCH = {16: "hooker", 17: "prop", 18: "prop"}
FORWARDS = {"prop", "hooker", "lock", "back row"}

# Labels printed in the SA/NZ announcements (used only when match data has no starts or bench).
SOURCE_LABELS = {
    "south_africa": {
        "prop": "Thomas du Toit;Ox Nche;Wilco Louw;Ntuthuko Mchunu;Zachary Porthen;Gerhard Steenekamp",
        "hooker": "Malcolm Marx;Jan-Hendrik Wessels",
        "lock": "Lood de Jager;Salmaan Moerat;Ruan Nortje;Riley Norton;Franco Mostert",
        "back row": "Paul de Villiers;Siya Kolisi;Cameron Hanekom;Emmanuel Tshituka;Jasper Wiese",
        "scrum-half": "Cobus Reinach;Morne van den Berg;Grant Williams",
        "fly-half": "Yaqeen Ahmed;Sacha Feinberg-Mngomezulu;Luan Giliomee;Manie Libbok",
        "centre": "Damian de Allende;Jesse Kriel;Markus Muller;Canan Moodie",
        "back three": "Sebastian de Klerk;Aphelele Fassi;Ethan Hooker;Edwill van der Merwe;Kurt-Lee Arendse;Damian Willemse",
    },
}


def norm(s):
    n = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return "".join(c.lower() for c in n if c.isalnum())


def read(p):
    with open(p, newline="") as f:
        return list(csv.DictReader(f))


def d(s):
    return datetime.strptime(s, "%d/%m/%Y").date()


def classify(starts, min_second=3, frac=0.3):
    """Most-started position; `hybrid` only if the player also starts across the
    forwards/backs divide (the other group >= frac of starts and >= min_second)."""
    c = collections.Counter(starts).most_common()
    if not c:
        return None
    fwd = sum(n for pos, n in c if pos in FORWARDS)
    bk = sum(n for pos, n in c if pos not in FORWARDS)
    if min(fwd, bk) >= min_second and min(fwd, bk) / (fwd + bk) >= frac:
        return "hybrid"
    return c[0][0]


def main():
    aliases = {(a["team"], norm(a["announced_name"])): norm(a["roster_name"])
               for a in read(ROOT / "data/raw/squads/aliases.csv")}
    labels = {(t, norm(p)): pos for t, g in SOURCE_LABELS.items()
              for pos, names in g.items() for p in names.split(";")}
    keep = {}
    if OUT.exists():
        keep = {(r["team"], r["player"]): r for r in read(OUT) if r["basis"].startswith("manual")}
    squads = read(EXPORT / "squads.csv")
    rows, seen = [], set()
    apps = {}
    for team in sorted({s["team"] for s in squads}):
        apps[team] = collections.defaultdict(list)
        for r in read(EXPORT / f"{team}_ages.csv"):
            apps[team][norm(r["player"])].append((d(r["date"]), int(r["shirt"])))
    for s in squads:
        team, player = s["team"], s["player"]
        if (team, player) in seen:
            continue
        seen.add((team, player))
        if (team, player) in keep:
            rows.append({"cover": "", **keep[(team, player)]})
            continue
        k = aliases.get((team, norm(player)), norm(player))
        a = apps[team].get(k, [])
        recent = [START[n] for dt, n in a if dt >= RECENT and n in START]
        every = [START[n] for dt, n in a if n in START]
        bench = [BENCH[n] for dt, n in a if n in BENCH]
        label = labels.get((team, k))
        if recent:
            pos, basis = classify(recent), f"starts since 2023 (n={len(recent)})"
        elif every:
            pos, basis = classify(every), f"starts since 2015 (n={len(every)})"
        elif bench:
            pos, basis = collections.Counter(bench).most_common(1)[0][0], f"bench shirts only (n={len(bench)})"
        elif label:
            pos, basis = label, "announcement label"
        else:
            pos, basis = "", "unknown"
        rows.append({"team": team, "player": player, "position": pos, "basis": basis, "cover": ""})
        # flag a clash with the announcement's forwards/backs split
        if pos and pos != "hybrid" and (pos in FORWARDS) != (s["position_group"] == "forwards"):
            rows[-1]["basis"] += " CLASH with " + s["position_group"]
    with open(OUT, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["team", "player", "position", "basis", "cover"])
        w.writeheader()
        w.writerows(rows)
    counts = collections.Counter(r["position"] or "UNKNOWN" for r in rows)
    print(f"positions.csv: {len(rows)} players", dict(counts))


if __name__ == "__main__":
    main()
