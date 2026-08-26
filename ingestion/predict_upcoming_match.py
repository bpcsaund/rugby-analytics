"""
Pre-match win-probability estimate for a specific upcoming international
fixture, using the same Elo/form/rank/coach-tenure state the training
pipeline computes (see build_intl_match_dataset.py) -- walked forward through
every match currently on file, then snapshotted for the named fixture instead
of a historical one.

Squad-level refinement: pass --home-squad / --away-squad (one player name per
line, matching build_squad_ages.py's naming) once teams are announced, and
average squad age/caps are computed from the actual named squad via each
team's *_roster.csv rather than the last-played-squad fallback.

Lineup-level refinement: pass --home-lineup / --away-lineup (one "shirt,name"
line per starter, shirts 1-15) once the starting XV is named, to compute real
positional-combination experience (front row, locks, half-backs, etc. -- see
COMBOS in build_intl_match_dataset.py) instead of a training-set median.

Usage:
    .venv/bin/python3 ingestion/predict_upcoming_match.py \
        --home south_africa --away new_zealand --date 2026-08-29

    # once teams/lineups are named:
    .venv/bin/python3 ingestion/predict_upcoming_match.py \
        --home south_africa --away new_zealand --date 2026-08-29 \
        --home-squad sa_squad.txt --away-squad nz_squad.txt \
        --home-lineup sa_lineup.txt --away-lineup nz_lineup.txt
"""

import argparse
import difflib
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge

from build_intl_match_dataset import (
    RAW_DIR, TEAMS, build_rows_and_state, combo_snapshot, snapshot,
)

FEATURE_ORDER = [
    "elo_diff", "form5_home", "form5_away", "pdiff5_home", "pdiff5_away",
    "rest_days_home", "rest_days_away", "rank_prev_home", "rank_prev_away",
    "coach_tenure_home", "coach_tenure_away", "avg_age_home", "avg_age_away",
    "combo_avg_home", "combo_avg_away", "combo_min_home", "combo_min_away",
]


def elo_win_prob(elo_diff: float) -> float:
    return 1 / (1 + 10 ** (-elo_diff / 400))


def squad_avg_age(team: str, squad_path: Path, match_date: pd.Timestamp) -> tuple[float, list[str]]:
    """Average age-on-matchday for the named squad, via {team}_roster.csv. Returns (avg_age, unmatched_names)."""
    roster = pd.read_csv(RAW_DIR / f"{team}_roster.csv")
    roster["player_lower"] = roster["player"].str.lower().str.strip()
    names = [n.strip() for n in squad_path.read_text().splitlines() if n.strip()]
    ages, unmatched = [], []
    for raw_name in names:
        name = raw_name.split(",", 1)[-1].strip() if "," in raw_name and raw_name.split(",")[0].strip().isdigit() else raw_name
        low = name.lower()
        match = roster[roster["player_lower"] == low]
        if match.empty:
            close = difflib.get_close_matches(low, roster["player_lower"], n=1, cutoff=0.75)
            match = roster[roster["player_lower"] == close[0]] if close else match
        if match.empty:
            unmatched.append(raw_name)
            continue
        dob = pd.to_datetime(match.iloc[0]["dob"], errors="coerce")
        if pd.isna(dob):
            unmatched.append(raw_name)
            continue
        ages.append((match_date - dob).days / 365.2425)
    return (float(np.mean(ages)) if ages else float("nan")), unmatched


def parse_lineup(lineup_path: Path) -> dict:
    """'shirt,name' per line -> {shirt: name.lower()}"""
    lineup = {}
    for line in lineup_path.read_text().splitlines():
        line = line.strip()
        if not line or "," not in line:
            continue
        shirt_str, name = line.split(",", 1)
        try:
            lineup[int(shirt_str.strip())] = name.strip().lower()
        except ValueError:
            continue
    return lineup


def latest_known_avg_age(team: str) -> float:
    df = pd.read_csv(RAW_DIR / "team_age_summary.csv")
    team_rows = df[df["team"] == team].copy()
    team_rows["date"] = pd.to_datetime(team_rows["date"], dayfirst=True, errors="coerce")
    team_rows = team_rows.dropna(subset=["date"]).sort_values("date")
    return float(team_rows.iloc[-1]["full_squad_avg_age"]) if len(team_rows) else float("nan")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", required=True, choices=TEAMS)
    ap.add_argument("--away", required=True, choices=TEAMS)
    ap.add_argument("--date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--home-squad", type=Path, help="text file, one player name per line")
    ap.add_argument("--away-squad", type=Path, help="text file, one player name per line")
    ap.add_argument("--home-lineup", type=Path, help="text file, 'shirt,name' per line (shirts 1-15)")
    ap.add_argument("--away-lineup", type=Path, help="text file, 'shirt,name' per line (shirts 1-15)")
    args = ap.parse_args()

    match_date = pd.Timestamp(args.date)

    train_rows, state = build_rows_and_state(asof=match_date - pd.Timedelta(days=1))
    snap_home = snapshot(state, args.home, match_date)
    snap_away = snapshot(state, args.away, match_date)

    if args.home_squad:
        age_home, unmatched_home = squad_avg_age(args.home, args.home_squad, match_date)
        if unmatched_home:
            print(f"WARNING: {len(unmatched_home)} {args.home} squad names not matched in roster: {unmatched_home}")
    else:
        age_home = latest_known_avg_age(args.home)
        print(f"No --home-squad given -- using {args.home}'s most recent known squad average age as a placeholder.")

    if args.away_squad:
        age_away, unmatched_away = squad_avg_age(args.away, args.away_squad, match_date)
        if unmatched_away:
            print(f"WARNING: {len(unmatched_away)} {args.away} squad names not matched in roster: {unmatched_away}")
    else:
        age_away = latest_known_avg_age(args.away)
        print(f"No --away-squad given -- using {args.away}'s most recent known squad average age as a placeholder.")

    if args.home_lineup:
        combo_home = combo_snapshot(state, args.home, parse_lineup(args.home_lineup))
    else:
        combo_home = {"combo_avg": float("nan"), "combo_min": float("nan")}
        print(f"No --home-lineup given -- combo-experience features for {args.home} will use the training median.")

    if args.away_lineup:
        combo_away = combo_snapshot(state, args.away, parse_lineup(args.away_lineup))
    else:
        combo_away = {"combo_avg": float("nan"), "combo_min": float("nan")}
        print(f"No --away-lineup given -- combo-experience features for {args.away} will use the training median.")

    feat = dict(
        elo_diff=snap_home["elo"] - snap_away["elo"],
        form5_home=snap_home["form5"], form5_away=snap_away["form5"],
        pdiff5_home=snap_home["pdiff5"], pdiff5_away=snap_away["pdiff5"],
        rest_days_home=snap_home["rest_days"], rest_days_away=snap_away["rest_days"],
        rank_prev_home=snap_home["rank_prev"], rank_prev_away=snap_away["rank_prev"],
        coach_tenure_home=snap_home["coach_tenure"], coach_tenure_away=snap_away["coach_tenure"],
        avg_age_home=age_home, avg_age_away=age_away,
        combo_avg_home=combo_home["combo_avg"], combo_avg_away=combo_away["combo_avg"],
        combo_min_home=combo_home["combo_min"], combo_min_away=combo_away["combo_min"],
    )

    print(f"\n{args.home} (home) vs {args.away} (away) -- {args.date}")
    print(f"  Elo:    {snap_home['elo']:.1f}  vs  {snap_away['elo']:.1f}  (diff {feat['elo_diff']:+.1f})")
    print(f"  Rank:   {snap_home['rank_prev']}  vs  {snap_away['rank_prev']}  (last known)")
    print(f"  Form(5 games): {snap_home['form5']:.2f}  vs  {snap_away['form5']:.2f}")
    print(f"  Avg squad age: {age_home:.1f}  vs  {age_away:.1f}")
    print(f"  Coach tenure:  {snap_home['coach_tenure']} games vs {snap_away['coach_tenure']} games")
    print(f"  Combo experience (avg times together): {combo_home['combo_avg']}  vs  {combo_away['combo_avg']}")

    # Scoreline regression: two Ridge models (home_score, away_score), retrained
    # on every tracked-vs-tracked match on file before this fixture. Ridge chosen
    # over XGBoost here since the backtest (train_intl_score_regression.py) showed
    # them roughly tied on MAE at this sample size, and Ridge is simpler/more stable.
    df = pd.DataFrame(train_rows).dropna(subset=["elo_diff"])
    X = df[FEATURE_ORDER]
    medians = X.median()
    X = X.fillna(medians)
    x_new = pd.DataFrame([feat])[FEATURE_ORDER].fillna(medians)

    home_model = Ridge(alpha=5.0).fit(X, df["home_score"])
    away_model = Ridge(alpha=5.0).fit(X, df["away_score"])
    pred_home_score = max(0.0, home_model.predict(x_new)[0])
    pred_away_score = max(0.0, away_model.predict(x_new)[0])
    margin = pred_home_score - pred_away_score

    print(f"\nProjected scoreline (Ridge regression, trained on {len(df)} matches to date):")
    print(f"  {args.home}: {pred_home_score:.0f}   {args.away}: {pred_away_score:.0f}   (margin {margin:+.1f})")
    print(f"  Backtested accuracy of this approach: MAE ~9 pts/team, ~13 pts on margin (see train_intl_score_regression.py)")
    print(f"  -> treat this as 'which side, roughly what gap', not a literal final score.")

    p_elo = elo_win_prob(feat["elo_diff"])
    print(f"\nFor reference, Elo-implied home-win probability: {p_elo:.1%}")


if __name__ == "__main__":
    main()
