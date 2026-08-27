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

Calibration log: pass --log (optionally with --market-line, the home team's
expected winning margin per the betting market -- e.g. "South Africa -7"
becomes --market-line 7) to append this prediction to
data/processed/calibration_log.csv. Run reconcile_calibration_log.py after the
fixture is played to fill in the actual result and see how our projection and
the market compare once results accumulate.

Usage:
    .venv/bin/python3 ingestion/predict_upcoming_match.py \
        --home south_africa --away new_zealand --date 2026-08-29

    # once teams/lineups are named, logging the prediction for later calibration:
    .venv/bin/python3 ingestion/predict_upcoming_match.py \
        --home south_africa --away new_zealand --date 2026-08-29 \
        --home-squad sa_squad.txt --away-squad nz_squad.txt \
        --home-lineup sa_lineup.txt --away-lineup nz_lineup.txt \
        --market-line 7 --log
"""

import argparse
import csv
import difflib
import math
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge

from build_intl_match_dataset import (
    RAW_DIR, TEAMS, build_rows_and_state, combo_snapshot, snapshot, squad_cohesion_snapshot,
)

LOG_PATH = Path(__file__).resolve().parent.parent / "data" / "processed" / "calibration_log.csv"
LOG_FIELDS = [
    "logged_at", "match_date", "home_team", "away_team",
    "elo_home", "elo_away", "elo_win_prob",
    "pred_home_score", "pred_away_score", "pred_margin",
    "market_margin", "model_weight", "blended_margin",
    "used_squad_data", "used_lineup_data",
    "actual_home_score", "actual_away_score", "actual_margin",
]

# Default weight on our own model when blending with the market line. Sports
# betting markets are efficient enough that a model rarely beats the closing
# line outright, so the market is the prior and our model is a shade on top:
#   blended = market + MODEL_WEIGHT * (model - market)
# Re-tune once reconcile_calibration_log.py has a dozen-plus resolved matches.
DEFAULT_MODEL_WEIGHT = 0.35

# Lean feature set. Walk-forward backtests (train_intl_*.py, and see git
# history) repeatedly found that Elo alone matched or beat the full ~22-feature
# model on the 2023-26 test period, and that only the venue loss-streak added
# consistent signal on top. Weather, cards, combo-experience, squad age, coach
# tenure, rest days, rank, rolling form, venue win-streaks and "form vs
# higher-ranked" are all still emitted as columns by build_intl_match_dataset.py
# for analysis -- they're just not fed to the model. FEATURE_ORDER_FULL keeps
# the old list for easy A/B.
FEATURE_ORDER = [
    "elo_diff", "venue_loss_streak_home", "venue_loss_streak_away",
]
FEATURE_ORDER_FULL = [
    "elo_diff", "neutral", "form5_home", "form5_away",
    "venue_form5_home", "venue_form5_away",
    "venue_loss_streak_home", "venue_loss_streak_away",
    "form_vs_stronger_home", "form_vs_stronger_away",
    "pdiff5_home", "pdiff5_away",
    "rest_days_home", "rest_days_away", "rank_prev_home", "rank_prev_away",
    "coach_tenure_home", "coach_tenure_away", "avg_age_home", "avg_age_away",
    "combo_avg_home", "combo_avg_away", "combo_min_home", "combo_min_away",
    "xv_cohesion_avg_home", "xv_cohesion_avg_away", "xv_cohesion_min_home", "xv_cohesion_min_away",
    "sq23_cohesion_avg_home", "sq23_cohesion_avg_away",
    "xv_retained_home", "xv_retained_away", "xv_starts_avg_home", "xv_starts_avg_away",
    "cards5_home", "cards5_away",
    "weather_temp_c", "weather_precip_mm", "weather_wind_kmh",
]


def forecast_weather(venue: str, city: str, country: str, match_date: pd.Timestamp, kickoff_hour: int) -> dict:
    """Live forecast (Open-Meteo, free) for a venue/date within its ~16-day forecast window."""
    import httpx
    from geocode_venues import geocode

    coords = geocode(venue, city, country)
    if not coords:
        print(f"WARNING: could not geocode venue '{venue}' -- weather features will use the training median.")
        return {"temp_c": float("nan"), "precip_mm": float("nan"), "wind_kmh": float("nan")}

    try:
        resp = httpx.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": coords["lat"], "longitude": coords["lon"],
            "start_date": match_date.strftime("%Y-%m-%d"), "end_date": match_date.strftime("%Y-%m-%d"),
            "hourly": "temperature_2m,precipitation,wind_speed_10m", "timezone": "auto",
        }, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        print(f"WARNING: forecast fetch failed ({e}) -- weather features will use the training median.")
        return {"temp_c": float("nan"), "precip_mm": float("nan"), "wind_kmh": float("nan")}

    target = f"{match_date.strftime('%Y-%m-%d')}T{kickoff_hour:02d}:00"
    times = data.get("hourly", {}).get("time", [])
    if target not in times:
        print(f"WARNING: {match_date.date()} is outside Open-Meteo's forecast window -- "
              f"weather features will use the training median.")
        return {"temp_c": float("nan"), "precip_mm": float("nan"), "wind_kmh": float("nan")}
    idx = times.index(target)
    hourly = data["hourly"]
    print(f"Forecast for {venue} ({coords['resolved_as']}) at {kickoff_hour:02d}:00 local on {match_date.date()}: "
          f"{hourly['temperature_2m'][idx]}°C, {hourly['precipitation'][idx]}mm precip, "
          f"{hourly['wind_speed_10m'][idx]}km/h wind")
    return {"temp_c": hourly["temperature_2m"][idx], "precip_mm": hourly["precipitation"][idx],
            "wind_kmh": hourly["wind_speed_10m"][idx]}


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
    ap.add_argument("--market-line", type=float,
                     help="home team's expected winning margin per the market (e.g. 'home -7' -> 7)")
    ap.add_argument("--model-weight", type=float, default=DEFAULT_MODEL_WEIGHT,
                     help=f"weight on our model vs the market line when blending (default {DEFAULT_MODEL_WEIGHT}; "
                          "0 = trust the market entirely, 1 = ignore it)")
    ap.add_argument("--log", action="store_true", help="append this prediction to data/processed/calibration_log.csv")
    ap.add_argument("--venue", help="venue name, for a live weather forecast (requires the match date be within ~16 days)")
    ap.add_argument("--city", default="", help="venue city, improves geocoding accuracy")
    ap.add_argument("--country", default="", help="venue country, improves geocoding accuracy")
    ap.add_argument("--kickoff-hour", type=int, default=15, help="local kickoff hour, 24h clock (default 15)")
    ap.add_argument("--neutral", action="store_true",
                     help="match is at a neutral venue (World Cup etc.) -- drops home advantage and venue-form features")
    ap.add_argument("--fetch-odds", action="store_true",
                     help="if --market-line not given, try The Odds API (Six Nations only -- see fetch_odds.py)")
    args = ap.parse_args()

    if args.market_line is None and args.fetch_odds:
        from fetch_odds import fetch_market_line
        got = fetch_market_line(args.home, args.away, args.date)
        if got:
            args.market_line = got["market_margin"]
            print(f"Market line {args.home} {args.market_line:+.1f} via {got['source']}")

    match_date = pd.Timestamp(args.date)

    train_rows, state = build_rows_and_state(asof=match_date - pd.Timedelta(days=1))
    snap_home = snapshot(state, args.home, match_date, is_home=None if args.neutral else True)
    snap_away = snapshot(state, args.away, match_date, is_home=None if args.neutral else False)

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

    NAN_COH = {"xv_cohesion_avg": float("nan"), "xv_cohesion_min": float("nan"),
               "sq23_cohesion_avg": float("nan"), "xv_retained": float("nan"),
               "xv_starts_avg": float("nan")}

    if args.home_lineup:
        lu_home = parse_lineup(args.home_lineup)
        combo_home = combo_snapshot(state, args.home, lu_home)
        coh_home = squad_cohesion_snapshot(state, args.home, {s: n for s, n in lu_home.items() if s <= 15}, lu_home)
    else:
        combo_home = {"combo_avg": float("nan"), "combo_min": float("nan")}
        coh_home = dict(NAN_COH)
        print(f"No --home-lineup given -- combo/cohesion features for {args.home} will use the training median.")

    if args.away_lineup:
        lu_away = parse_lineup(args.away_lineup)
        combo_away = combo_snapshot(state, args.away, lu_away)
        coh_away = squad_cohesion_snapshot(state, args.away, {s: n for s, n in lu_away.items() if s <= 15}, lu_away)
    else:
        combo_away = {"combo_avg": float("nan"), "combo_min": float("nan")}
        coh_away = dict(NAN_COH)
        print(f"No --away-lineup given -- combo/cohesion features for {args.away} will use the training median.")

    if args.venue:
        weather = forecast_weather(args.venue, args.city, args.country, match_date, args.kickoff_hour)
    else:
        weather = {"temp_c": float("nan"), "precip_mm": float("nan"), "wind_kmh": float("nan")}
        print("No --venue given -- weather features will use the training median.")

    feat = dict(
        elo_diff=snap_home["elo"] - snap_away["elo"],
        neutral=int(args.neutral),
        form5_home=snap_home["form5"], form5_away=snap_away["form5"],
        venue_form5_home=snap_home["venue_form5"], venue_form5_away=snap_away["venue_form5"],
        venue_loss_streak_home=snap_home["venue_loss_streak"], venue_loss_streak_away=snap_away["venue_loss_streak"],
        form_vs_stronger_home=snap_home["form_vs_stronger"], form_vs_stronger_away=snap_away["form_vs_stronger"],
        pdiff5_home=snap_home["pdiff5"], pdiff5_away=snap_away["pdiff5"],
        rest_days_home=snap_home["rest_days"], rest_days_away=snap_away["rest_days"],
        rank_prev_home=snap_home["rank_prev"], rank_prev_away=snap_away["rank_prev"],
        coach_tenure_home=snap_home["coach_tenure"], coach_tenure_away=snap_away["coach_tenure"],
        avg_age_home=age_home, avg_age_away=age_away,
        combo_avg_home=combo_home["combo_avg"], combo_avg_away=combo_away["combo_avg"],
        combo_min_home=combo_home["combo_min"], combo_min_away=combo_away["combo_min"],
        xv_cohesion_avg_home=coh_home["xv_cohesion_avg"], xv_cohesion_avg_away=coh_away["xv_cohesion_avg"],
        xv_cohesion_min_home=coh_home["xv_cohesion_min"], xv_cohesion_min_away=coh_away["xv_cohesion_min"],
        sq23_cohesion_avg_home=coh_home["sq23_cohesion_avg"], sq23_cohesion_avg_away=coh_away["sq23_cohesion_avg"],
        xv_retained_home=coh_home["xv_retained"], xv_retained_away=coh_away["xv_retained"],
        xv_starts_avg_home=coh_home["xv_starts_avg"], xv_starts_avg_away=coh_away["xv_starts_avg"],
        cards5_home=snap_home["cards5"], cards5_away=snap_away["cards5"],
        weather_temp_c=weather["temp_c"], weather_precip_mm=weather["precip_mm"], weather_wind_kmh=weather["wind_kmh"],
    )

    print(f"\n{args.home} (home) vs {args.away} (away) -- {args.date}")
    print("  Model inputs:")
    print(f"    Elo:  {snap_home['elo']:.1f}  vs  {snap_away['elo']:.1f}  (diff {feat['elo_diff']:+.1f})")
    print(f"    Consecutive venue losses coming in:  {snap_home['venue_loss_streak']}  vs  {snap_away['venue_loss_streak']}")
    print("  Context (computed, not fed to the model -- see FEATURE_ORDER_FULL):")
    print(f"    Rank (last known):  {snap_home['rank_prev']}  vs  {snap_away['rank_prev']}")
    print(f"    Form / venue form / vs-stronger (last 5):  "
          f"{snap_home['form5']:.2f}/{snap_home['venue_form5']:.2f}/{snap_home['form_vs_stronger']:.2f}  vs  "
          f"{snap_away['form5']:.2f}/{snap_away['venue_form5']:.2f}/{snap_away['form_vs_stronger']:.2f}")
    print(f"    Avg squad age:  {age_home:.1f}  vs  {age_away:.1f}")
    print(f"    Coach tenure:  {snap_home['coach_tenure']} vs {snap_away['coach_tenure']} games")
    print(f"    Combo experience (avg times together):  {combo_home['combo_avg']}  vs  {combo_away['combo_avg']}")
    print(f"    XV cohesion (avg prior co-starts / 105 pairs):  {coh_home['xv_cohesion_avg']}  vs  {coh_away['xv_cohesion_avg']}")
    print(f"    23 cohesion (avg prior co-selections):  {coh_home['sq23_cohesion_avg']}  vs  {coh_away['sq23_cohesion_avg']}")
    print(f"    XV retained from last Test / avg XV starts:  "
          f"{coh_home['xv_retained']}/{coh_home['xv_starts_avg']}  vs  {coh_away['xv_retained']}/{coh_away['xv_starts_avg']}")
    print(f"    Cards conceded (avg last 5):  {snap_home['cards5']}  vs  {snap_away['cards5']}")
    print(f"    Weather:  {weather['temp_c']}C, {weather['precip_mm']}mm precip, {weather['wind_kmh']}km/h wind")

    # Scoreline regression: two Ridge models (home_score, away_score) on the
    # LEAN feature set, retrained on every tracked-vs-tracked match on file
    # before this fixture. On the 2024-26 backtest the lean Ridge margin MAE
    # (~12.4) is close to the raw elo_diff/25 heuristic (~12.1) and clearly
    # better than the full-feature Ridge (~13.3) -- see train_intl_score_regression.py.
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
    print(f"  Backtested accuracy of this approach: MAE ~9 pts/team, ~12 pts on margin (see train_intl_score_regression.py)")
    print(f"  -> treat this as 'which side, roughly what gap', not a literal final score.")

    p_elo = elo_win_prob(feat["elo_diff"])
    print(f"\nFor reference, Elo-implied home-win probability: {p_elo:.1%}")

    blended_margin = None
    if args.market_line is not None:
        blended_margin = args.market_line + args.model_weight * (margin - args.market_line)
        # rough win prob from a margin, using the ~15pt margin RMSE from the backtest
        p_blend = 0.5 * (1 + math.erf(blended_margin / (15.0 * math.sqrt(2))))
        print(f"\n  Model margin:    {args.home} {margin:+.1f}")
        print(f"  Market line:     {args.home} {args.market_line:+.1f}")
        print(f"  Blended ({args.model_weight:.2f} model / {1 - args.model_weight:.2f} market):  "
              f"{args.home} {blended_margin:+.1f}   (~{p_blend:.0%} {args.home} win)")
        gap = margin - args.market_line
        print(f"  We are {abs(gap):.1f} pts {'below' if gap < 0 else 'above'} the market on {args.home}.")

    if args.log:
        log_row = dict(
            logged_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            match_date=args.date, home_team=args.home, away_team=args.away,
            elo_home=round(snap_home["elo"], 1), elo_away=round(snap_away["elo"], 1),
            elo_win_prob=round(p_elo, 4),
            pred_home_score=round(pred_home_score, 1), pred_away_score=round(pred_away_score, 1),
            pred_margin=round(margin, 1),
            market_margin=args.market_line,
            model_weight=args.model_weight if args.market_line is not None else "",
            blended_margin=round(blended_margin, 1) if blended_margin is not None else "",
            used_squad_data=bool(args.home_squad or args.away_squad),
            used_lineup_data=bool(args.home_lineup or args.away_lineup),
            actual_home_score="", actual_away_score="", actual_margin="",
        )
        write_header = not LOG_PATH.exists()
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
            if write_header:
                writer.writeheader()
            writer.writerow(log_row)
        print(f"\nLogged to {LOG_PATH} -- run reconcile_calibration_log.py after the match to fill in the result.")


if __name__ == "__main__":
    main()
