"""Rotation/attrition and minutes-usage comparison across 5 specific coach
tenures: Andy Farrell (Ireland), Steve Borthwick (England), Fabien Galthie
(France), Rassie Erasmus's SECOND stint (South Africa, from 22/06/2024 -
the coach code 're' is reused for his first 2019 stint too, so this is
date-filtered, not just code-filtered), Gregor Townsend (Scotland).

Outputs a single JSON blob (coach_usage_metrics.json) consumed by the
comparison dashboard artifact.
"""
import csv
import json
from datetime import datetime
from pathlib import Path

OUT_DIR = Path.home() / "rugby_analytics/data/raw/sheets_export"

COACHES = [
    {"key": "farrell", "label": "Farrell (IRE)", "team": "ireland", "coach_code": "af", "min_date": None},
    {"key": "borthwick", "label": "Borthwick (ENG)", "team": "england", "coach_code": "sb", "min_date": None},
    {"key": "galthie", "label": "Galthié (FRA)", "team": "france", "coach_code": "fg", "min_date": None},
    {"key": "erasmus2", "label": "Erasmus II (RSA)", "team": "south_africa", "coach_code": "re", "min_date": "2024-06-22"},
    {"key": "townsend", "label": "Townsend (SCO)", "team": "scotland", "coach_code": "gt", "min_date": None},
]

LINEUP_COL_START = {"ireland": 15, "france": 15, "south_africa": 15, "scotland": 15, "england": 35}
COACH_COL = {"ireland": 2, "france": 2, "south_africa": 2, "scotland": 2, "england": 33}
OPP_COL = {"ireland": 3, "france": 3, "south_africa": 3, "scotland": 3, "england": 2}


def to_iso(d_m_y: str) -> str:
    d, m, y = d_m_y.split("/")
    return f"{y}-{m}-{d}"


def load_tenure_matches(coach: dict) -> list[dict]:
    team = coach["team"]
    path = OUT_DIR / f"{team}_rugby.csv"
    lineup_start = LINEUP_COL_START[team]
    coach_col = COACH_COL[team]
    opp_col = OPP_COL[team]
    rows = list(csv.reader(open(path, newline="")))
    header, body = rows[0], rows[1:]
    out = []
    for row in body:
        if len(row) <= max(lineup_start + 22, coach_col):
            continue
        if row[0] == "" or row[coach_col] != coach["coach_code"]:
            continue
        if coach["min_date"] and to_iso(row[0]) < coach["min_date"]:
            continue
        lineup = row[lineup_start:lineup_start + 23]
        out.append({"date": row[0], "opposition": row[opp_col], "lineup": lineup})
    out.sort(key=lambda r: to_iso(r["date"]))
    return out


def load_minutes(team: str) -> dict:
    """date -> list of {shirt, player, started, minutes}"""
    path = OUT_DIR / f"{team}_minutes.csv"
    by_date = {}
    for row in csv.DictReader(open(path, newline="")):
        by_date.setdefault(row["date"], []).append(row)
    return by_date


def rotation_metrics(matches: list[dict]) -> dict:
    appearance_count = {}
    start_count = {}
    for m in matches:
        for i, name in enumerate(m["lineup"]):
            name = name.strip()
            if not name:
                continue
            appearance_count[name] = appearance_count.get(name, 0) + 1
            if i < 15:
                start_count[name] = start_count.get(name, 0) + 1

    unique_players = len(appearance_count)
    one_cap_wonders = sum(1 for c in appearance_count.values() if c == 1)

    # starting-XV week-to-week turnover
    turnovers = []
    prev_xv = None
    for m in matches:
        xv = set(n.strip() for n in m["lineup"][:15] if n.strip())
        if prev_xv is not None and len(xv) == 15 and len(prev_xv) == 15:
            changed = len(xv - prev_xv)
            turnovers.append(changed)
        prev_xv = xv
    avg_turnover = round(sum(turnovers) / len(turnovers), 2) if turnovers else None

    # core-15 share of total start-slots
    total_start_slots = len(matches) * 15
    top15_starts = sum(sorted(start_count.values(), reverse=True)[:15])
    core15_share = round(100 * top15_starts / total_start_slots, 1) if total_start_slots else None

    return {
        "matches": len(matches),
        "unique_players_used": unique_players,
        "one_cap_wonders": one_cap_wonders,
        "avg_starting_xv_changes_per_match": avg_turnover,
        "core15_share_of_starts_pct": core15_share,
    }


def usage_metrics(matches: list[dict], minutes_by_date: dict) -> dict:
    bench_minutes_per_match = []
    bench_unused_frac = []
    starter_minutes = []
    for m in matches:
        recs = minutes_by_date.get(m["date"])
        if not recs:
            continue
        bench = [r for r in recs if int(r["shirt"]) > 15]
        starters = [r for r in recs if int(r["shirt"]) <= 15]
        if bench:
            used = [int(r["minutes"]) for r in bench]
            bench_minutes_per_match.append(sum(used))
            bench_unused_frac.append(sum(1 for v in used if v == 0) / len(used))
        if starters:
            starter_minutes.extend(int(r["minutes"]) for r in starters)

    return {
        "avg_bench_minutes_per_match": round(sum(bench_minutes_per_match) / len(bench_minutes_per_match), 1) if bench_minutes_per_match else None,
        "avg_pct_bench_unused": round(100 * sum(bench_unused_frac) / len(bench_unused_frac), 1) if bench_unused_frac else None,
        "avg_starter_minutes": round(sum(starter_minutes) / len(starter_minutes), 1) if starter_minutes else None,
        "matches_with_minutes_data": len(bench_minutes_per_match),
    }


def main():
    results = {}
    for coach in COACHES:
        matches = load_tenure_matches(coach)
        minutes_by_date = load_minutes(coach["team"])
        rot = rotation_metrics(matches)
        use = usage_metrics(matches, minutes_by_date)
        date_range = [matches[0]["date"], matches[-1]["date"]] if matches else None
        results[coach["key"]] = {
            "label": coach["label"],
            "team": coach["team"],
            "date_range": date_range,
            **rot,
            **use,
        }
        print(coach["label"], json.dumps(results[coach["key"]], ensure_ascii=False))

    out_file = OUT_DIR / "coach_usage_metrics.json"
    out_file.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    print(f"\nWritten to {out_file}")


if __name__ == "__main__":
    main()
