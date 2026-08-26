"""
Build a leakage-free, pre-match feature dataset for international rugby match
prediction, from the 10 national teams' wide-format CSVs in
data/raw/sheets_export/.

Only matches between two of the 10 tracked teams are emitted as training rows
(both sides then have full feature coverage). Matches against untracked
opponents (Fiji, Georgia, Japan, etc.) are still walked in chronological order
to keep each tracked team's Elo/form/rest state accurate -- they just aren't
emitted as rows, since the untracked side has no comparable feature history.

All emitted features are computed strictly from data available *before* the
match (previous matches only) -- none of the raw CSVs' own-match box-score
columns (tries, kick %, possession, etc.) are used, since those are outcomes
of the match itself.

Output: data/processed/intl_match_features.csv
"""

import csv
import math
from collections import defaultdict, deque
from pathlib import Path

import numpy as np
import pandas as pd

RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw" / "sheets_export"
OUT_PATH = Path(__file__).resolve().parent.parent / "data" / "processed" / "intl_match_features.csv"

TEAMS = [
    "england", "ireland", "france", "australia", "wales",
    "scotland", "italy", "new_zealand", "south_africa", "argentina",
]

# Maps opposition-column text (lowercase) -> canonical team id, for the 10
# tracked teams. Anything not in this dict is an untracked opponent and is
# kept as its own free-text id.
ALIASES = {
    "england": "england",
    "ireland": "ireland",
    "france": "france",
    "australia": "australia",
    "aus": "australia",
    "wales": "wales",
    "scotland": "scotland",
    "italy": "italy",
    "new zealand": "new_zealand",
    "nz": "new_zealand",
    "south africa": "south_africa",
    "sa": "south_africa",
    "argentina": "argentina",
}

HOME_ADV = 60          # Elo points of home advantage baked into expected score
K_BASE = 20            # base Elo K-factor
FORM_WINDOW = 5        # matches for rolling form/point-diff features

# Positional combinations tracked for "experience playing together" -- shirt
# numbers follow standard rugby positional numbering (1=loosehead prop ...
# 15=fullback), which is consistent across all matches in these CSVs.
COMBOS = {
    "front_row": (1, 2, 3),
    "locks": (4, 5),
    "back_row": (6, 7, 8),
    "lineout_unit": (2, 4, 5, 8),
    "halfbacks": (9, 10),
    "inside_backs": (10, 12, 13),
    "back_three": (11, 14, 15),
    "spine": (2, 4, 5, 8, 9, 10, 12, 15),
}


def normalize(name: str) -> str:
    return ALIASES.get(name.strip().lower(), name.strip().lower())


def safe_str(v) -> str:
    return "" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v).strip()


def find_col(columns, *substrings):
    for c in columns:
        low = c.lower()
        if all(s in low for s in substrings):
            return c
    return None


def load_team_events(team: str) -> list[dict]:
    """One row per match from `team`'s own CSV, as a plain dict event."""
    df = pd.read_csv(RAW_DIR / f"{team}_rugby.csv", dtype=str, keep_default_na=True)
    df.columns = [c.strip() for c in df.columns]

    col_result = find_col(df.columns, "result")
    col_ha = find_col(df.columns, "h", "/", "a") or find_col(df.columns, "h / a")
    col_own_irb = next(
        (c for c in df.columns if c.lower().endswith("irb") and "opp" not in c.lower()), None
    )
    col_opp_irb = find_col(df.columns, "opp", "irb")
    col_scored = next((c for c in df.columns if "scored" in c.lower()), None)
    col_conceded = next((c for c in df.columns if "conceded" in c.lower()), None)
    col_coach = "coach" if "coach" in df.columns else None
    col_tournament = "tournament" if "tournament" in df.columns else None
    col_test = "test" if "test" in df.columns else None

    events = []
    for _, row in df.iterrows():
        date = pd.to_datetime(row.get("date"), dayfirst=True, errors="coerce")
        opp_raw = safe_str(row.get("opposition"))
        result = safe_str(row.get(col_result)).lower()
        scored = row.get(col_scored)
        conceded = row.get(col_conceded)
        ha = safe_str(row.get(col_ha)).lower()

        if pd.isna(date) or not opp_raw or result not in ("w", "l", "d"):
            continue
        try:
            scored_v = float(scored)
            conceded_v = float(conceded)
        except (TypeError, ValueError):
            continue
        if math.isnan(scored_v) or math.isnan(conceded_v):
            continue

        test_flag = safe_str(row.get(col_test)).lower() if col_test else "yes"
        if test_flag == "no":
            continue

        lineup = {}
        for shirt in range(1, 16):
            name = safe_str(row.get(str(shirt))).lower()
            if name:
                lineup[shirt] = name

        events.append(dict(
            date=date,
            team=team,
            opponent=normalize(opp_raw),
            home_away=ha,
            result=result,
            points_for=scored_v,
            points_against=conceded_v,
            tournament=safe_str(row.get(col_tournament)).lower() if col_tournament else "",
            coach=safe_str(row.get(col_coach)).lower() if col_coach else "",
            own_irb=pd.to_numeric(row.get(col_own_irb), errors="coerce") if col_own_irb else float("nan"),
            lineup=lineup,
        ))
    return events


def load_cards_lookup() -> dict:
    """(team, date, normalized_opposition) -> weighted card score (yellow + 2*red) for that match."""
    path = RAW_DIR / "match_cards.csv"
    if not path.exists():
        return {}
    df = pd.read_csv(path, dtype=str)
    lookup = {}
    for _, row in df.iterrows():
        date = pd.to_datetime(row["date"], dayfirst=True, errors="coerce")
        if pd.isna(date):
            continue
        key = (row["team"], date, normalize(row["opposition"]))
        try:
            lookup[key] = float(row["yellow_cards"]) + 2 * float(row["red_cards"])
        except (TypeError, ValueError):
            continue
    return lookup


def load_age_lookup() -> dict:
    """(team, date, normalized_opposition) -> full_squad_avg_age"""
    df = pd.read_csv(RAW_DIR / "team_age_summary.csv", dtype=str)
    lookup = {}
    for _, row in df.iterrows():
        date = pd.to_datetime(row["date"], dayfirst=True, errors="coerce")
        if pd.isna(date):
            continue
        key = (row["team"], date, normalize(row["opposition"]))
        try:
            lookup[key] = float(row["full_squad_avg_age"])
        except (TypeError, ValueError):
            continue
    return lookup


def dedupe_events(all_events: list[dict]) -> list[dict]:
    """
    Collapse the two rows produced for a tracked-vs-tracked match (one from
    each side's file) into a single match record. Everything else passes
    through as a single-sided record against an untracked opponent.
    """
    by_key = defaultdict(list)
    singles = []
    for ev in all_events:
        if ev["opponent"] in TEAMS:
            key = (ev["date"], frozenset({ev["team"], ev["opponent"]}))
            by_key[key].append(ev)
        else:
            singles.append(ev)

    matches = []
    for (date, pair), evs in by_key.items():
        if len(evs) == 1:
            # Other side's file didn't record this one (a data gap) -- keep
            # it single-sided so Elo/form for the recorded side still updates.
            singles.append(evs[0])
            continue
        a, b = evs[0], evs[1]
        if a["home_away"] == "h" and b["home_away"] != "h":
            home, away = a, b
        elif b["home_away"] == "h" and a["home_away"] != "h":
            home, away = b, a
        else:
            # both marked away/neutral (World Cup etc.) -- pick alphabetically
            home, away = sorted((a, b), key=lambda e: e["team"])
        matches.append(dict(
            date=date, kind="pair",
            home_team=home["team"], away_team=away["team"],
            home_score=home["points_for"], away_score=home["points_against"],
            tournament=home["tournament"] or away["tournament"],
            home_coach=home["coach"], away_coach=away["coach"],
            home_irb=home["own_irb"], away_irb=away["own_irb"],
            home_lineup=home["lineup"], away_lineup=away["lineup"],
        ))
    for ev in singles:
        matches.append(dict(
            date=ev["date"], kind="single",
            team=ev["team"], opponent=ev["opponent"],
            points_for=ev["points_for"], points_against=ev["points_against"],
            home_away=ev["home_away"], tournament=ev["tournament"],
            coach=ev["coach"], own_irb=ev["own_irb"], lineup=ev["lineup"],
        ))
    matches.sort(key=lambda m: m["date"])
    return matches


def mov_multiplier(point_diff: float, elo_diff_winner: float) -> float:
    """FiveThirtyEight-style margin-of-victory dampener, floored at 1 match's worth."""
    return math.log(abs(point_diff) + 1) * (2.2 / (elo_diff_winner * 0.001 + 2.2))


def update_elo(elo: dict, team_a: str, team_b: str, score_a: float, score_b: float, a_is_home: bool):
    ra, rb = elo[team_a], elo[team_b]
    home_bonus = HOME_ADV if a_is_home else -HOME_ADV if not a_is_home else 0
    exp_a = 1 / (1 + 10 ** (-((ra + home_bonus) - rb) / 400))
    actual_a = 0.5 if score_a == score_b else (1.0 if score_a > score_b else 0.0)
    winner_elo_diff = abs(ra - rb) if score_a != score_b else 0
    mult = mov_multiplier(score_a - score_b, winner_elo_diff) if score_a != score_b else 1.0
    mult = max(mult, 0.5)
    delta = K_BASE * mult * (actual_a - exp_a)
    elo[team_a] = ra + delta
    elo[team_b] = rb - delta


def new_state() -> dict:
    """Fresh per-team state used while walking matches in chronological order."""
    return dict(
        elo=defaultdict(lambda: 1500.0),
        recent_results=defaultdict(lambda: deque(maxlen=FORM_WINDOW)),   # 1/0.5/0
        recent_point_diff=defaultdict(lambda: deque(maxlen=FORM_WINDOW)),
        recent_cards=defaultdict(lambda: deque(maxlen=FORM_WINDOW)),   # only matches with known card data
        last_match_date={},
        last_irb={},
        coach_state={},   # team -> (coach_code, games_played_under_coach)
        games_played=defaultdict(int),
        # team -> combo_name -> {frozenset(player names): times started together before now}
        combo_counts=defaultdict(lambda: defaultdict(lambda: defaultdict(int))),
    )


def combo_snapshot(state: dict, team: str, lineup: dict) -> dict:
    """
    Prior-appearances-together count for each tracked combo, given `lineup`
    (shirt number -> player name). NaN for a combo if any required shirt is
    missing from the lineup (partial/legacy data).
    """
    counts = {}
    for combo_name, shirts in COMBOS.items():
        if not all(s in lineup for s in shirts):
            counts[combo_name] = float("nan")
            continue
        group = frozenset(lineup[s] for s in shirts)
        counts[combo_name] = state["combo_counts"][team][combo_name][group]
    valid = [v for v in counts.values() if not pd.isna(v)]
    counts["combo_avg"] = float(np.mean(valid)) if valid else float("nan")
    counts["combo_min"] = float(np.min(valid)) if valid else float("nan")
    return counts


def combo_advance(state: dict, team: str, lineup: dict):
    """Record this match's combos as having been played, for future lookups."""
    for combo_name, shirts in COMBOS.items():
        if not all(s in lineup for s in shirts):
            continue
        group = frozenset(lineup[s] for s in shirts)
        state["combo_counts"][team][combo_name][group] += 1


def snapshot(state: dict, team: str, date) -> dict:
    """Pre-match feature snapshot for `team` as of (but not including) `date`."""
    coach_code, tenure = state["coach_state"].get(team, (None, 0))
    recent_results = state["recent_results"][team]
    recent_point_diff = state["recent_point_diff"][team]
    recent_cards = state["recent_cards"][team]
    return dict(
        elo=state["elo"][team],
        form5=(sum(recent_results) / len(recent_results)) if recent_results else float("nan"),
        pdiff5=(sum(recent_point_diff) / len(recent_point_diff)) if recent_point_diff else float("nan"),
        cards5=(sum(recent_cards) / len(recent_cards)) if recent_cards else float("nan"),
        rest_days=(date - state["last_match_date"][team]).days if team in state["last_match_date"] else float("nan"),
        rank_prev=state["last_irb"].get(team, float("nan")),
        coach_tenure=tenure,
        n_hist=state["games_played"][team],
    )


def advance(state: dict, team: str, date, result: str, point_diff: float, coach_code: str, own_irb: float,
            card_score: float | None = None):
    prev_coach, tenure = state["coach_state"].get(team, (None, 0))
    if coach_code and coach_code != prev_coach:
        tenure = 0
    state["coach_state"][team] = (coach_code or prev_coach, tenure + 1)
    state["recent_results"][team].append(1.0 if result == "w" else 0.5 if result == "d" else 0.0)
    state["recent_point_diff"][team].append(point_diff)
    if card_score is not None:
        state["recent_cards"][team].append(card_score)
    state["last_match_date"][team] = date
    if not pd.isna(own_irb):
        state["last_irb"][team] = own_irb
    state["games_played"][team] += 1


def build_rows_and_state(asof: pd.Timestamp | None = None) -> tuple[list[dict], dict]:
    """
    Walk every tracked team's match history in chronological order (optionally
    capped at `asof`), emitting a training row per tracked-vs-tracked match and
    returning the final per-team state (Elo, form, rest, coach tenure, etc.) as
    of the last processed match -- the same state a pre-match prediction for
    the *next* fixture would snapshot from.
    """
    all_events = []
    for team in TEAMS:
        all_events.extend(load_team_events(team))
    matches = dedupe_events(all_events)
    if asof is not None:
        matches = [m for m in matches if m["date"] <= asof]
    age_lookup = load_age_lookup()
    cards_lookup = load_cards_lookup()
    state = new_state()
    elo = state["elo"]

    rows = []
    for m in matches:
        date = m["date"]
        if m["kind"] == "single":
            team, opp = m["team"], m["opponent"]
            result = "w" if m["points_for"] > m["points_against"] else ("d" if m["points_for"] == m["points_against"] else "l")
            is_home = m["home_away"] == "h"
            update_elo(elo, team, opp, m["points_for"], m["points_against"], a_is_home=is_home)
            card_score = cards_lookup.get((team, date, opp))
            advance(state, team, date, result, m["points_for"] - m["points_against"], m["coach"], m["own_irb"], card_score)
            combo_advance(state, team, m["lineup"])
            continue

        home, away = m["home_team"], m["away_team"]
        snap_home = snapshot(state, home, date)
        snap_away = snapshot(state, away, date)
        combo_home = combo_snapshot(state, home, m["home_lineup"])
        combo_away = combo_snapshot(state, away, m["away_lineup"])
        age_home = age_lookup.get((home, date, away))
        age_away = age_lookup.get((away, date, home))

        home_result = "w" if m["home_score"] > m["away_score"] else ("d" if m["home_score"] == m["away_score"] else "l")
        rows.append(dict(
            date=date.strftime("%Y-%m-%d"),
            tournament=m["tournament"],
            home_team=home, away_team=away,
            home_score=m["home_score"], away_score=m["away_score"],
            result=home_result,
            elo_home=round(snap_home["elo"], 1), elo_away=round(snap_away["elo"], 1),
            elo_diff=round(snap_home["elo"] - snap_away["elo"], 1),
            form5_home=snap_home["form5"], form5_away=snap_away["form5"],
            pdiff5_home=snap_home["pdiff5"], pdiff5_away=snap_away["pdiff5"],
            rest_days_home=snap_home["rest_days"], rest_days_away=snap_away["rest_days"],
            rank_prev_home=snap_home["rank_prev"], rank_prev_away=snap_away["rank_prev"],
            coach_tenure_home=snap_home["coach_tenure"], coach_tenure_away=snap_away["coach_tenure"],
            avg_age_home=age_home, avg_age_away=age_away,
            n_hist_home=snap_home["n_hist"], n_hist_away=snap_away["n_hist"],
            combo_avg_home=combo_home["combo_avg"], combo_avg_away=combo_away["combo_avg"],
            combo_min_home=combo_home["combo_min"], combo_min_away=combo_away["combo_min"],
            **{f"combo_{name}_home": combo_home[name] for name in COMBOS},
            **{f"combo_{name}_away": combo_away[name] for name in COMBOS},
            cards5_home=snap_home["cards5"], cards5_away=snap_away["cards5"],
        ))

        # advance both sides' state after snapshotting
        update_elo(elo, home, away, m["home_score"], m["away_score"], a_is_home=True)
        card_home = cards_lookup.get((home, date, away))
        card_away = cards_lookup.get((away, date, home))
        advance(state, home, date, home_result, m["home_score"] - m["away_score"], m["home_coach"], m["home_irb"], card_home)
        advance(state, away, date, "l" if home_result == "w" else ("d" if home_result == "d" else "w"),
                m["away_score"] - m["home_score"], m["away_coach"], m["away_irb"], card_away)
        combo_advance(state, home, m["home_lineup"])
        combo_advance(state, away, m["away_lineup"])

    return rows, state


def main():
    rows, _ = build_rows_and_state()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_PATH, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {len(rows)} tracked-vs-tracked matches to {OUT_PATH}")
    print(f"Date range: {rows[0]['date']} .. {rows[-1]['date']}")
    from collections import Counter
    print("Result balance:", Counter(r["result"] for r in rows))
    n_missing_age = sum(1 for r in rows if r["avg_age_home"] is None or r["avg_age_away"] is None)
    print(f"Rows missing squad-age join on one side: {n_missing_age}/{len(rows)}")


if __name__ == "__main__":
    main()
