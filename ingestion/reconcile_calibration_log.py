"""
Fills in actual results for rows logged by predict_upcoming_match.py --log,
then reports how our projected margin compares to the betting market's line
(where logged) once results are known.

Run this periodically (e.g. after each weekend's Tests) once fixtures logged
via --log have been played. Unresolved rows are looked up first against the
local dataset, then -- so this works standalone in a fresh checkout with no
data refresh -- directly against the World Rugby match API.
"""

from pathlib import Path

import httpx
import numpy as np
import pandas as pd

from build_intl_match_dataset import build_rows_and_state

LOG_PATH = Path(__file__).resolve().parent.parent / "data" / "processed" / "calibration_log.csv"
WR_MATCH_API = "https://api.wr-rims-prod.pulselive.com/rugby/v3/match"


def _wr_name(team_id: str) -> str:
    return team_id.replace("_", " ").title()


def fetch_result_from_wr(match_date: str, home_team: str, away_team: str) -> tuple | None:
    """(home_score, away_score) for a completed Test, via the WR bulk match feed. None if not found/not complete."""
    day = pd.to_datetime(match_date)
    want = {_wr_name(home_team), _wr_name(away_team)}
    try:
        r = httpx.get(WR_MATCH_API, params={
            "states": "C", "sort": "asc", "pageSize": 100, "sport": "mru",
            "startDate": (day - pd.Timedelta(days=2)).strftime("%Y-%m-%d"),
            "endDate": (day + pd.Timedelta(days=2)).strftime("%Y-%m-%d"),
        }, timeout=30)
        r.raise_for_status()
        content = r.json().get("content", [])
    except (httpx.HTTPError, ValueError) as e:
        print(f"  WR API lookup failed for {home_team} v {away_team} {match_date}: {e}")
        return None
    for m in content:
        names = {t["name"] for t in m.get("teams", [])}
        if names != want or m.get("status") != "C":
            continue
        by_name = {t["name"]: s for t, s in zip(m["teams"], m.get("scores", []))}
        if _wr_name(home_team) in by_name and _wr_name(away_team) in by_name:
            return float(by_name[_wr_name(home_team)]), float(by_name[_wr_name(away_team)])
    return None


def main():
    if not LOG_PATH.exists():
        print(f"No calibration log yet at {LOG_PATH} -- run predict_upcoming_match.py with --log first.")
        return

    log = pd.read_csv(LOG_PATH, dtype={"match_date": str, "home_team": str, "away_team": str})
    for c in ("pred_margin", "market_margin", "blended_margin", "model_weight",
              "actual_margin", "actual_home_score", "actual_away_score"):
        if c in log.columns:
            log[c] = pd.to_numeric(log[c], errors="coerce")
    results, _ = build_rows_and_state()
    results_df = pd.DataFrame(results)

    unresolved = log["actual_home_score"].isna()
    n_filled = 0
    for i in log[unresolved].index:
        row = log.loc[i]
        match = results_df[
            (results_df["date"] == row["match_date"])
            & (results_df["home_team"] == row["home_team"])
            & (results_df["away_team"] == row["away_team"])
        ]
        if not match.empty:
            m = match.iloc[0]
            hs, as_ = float(m["home_score"]), float(m["away_score"])
        else:
            wr = fetch_result_from_wr(row["match_date"], row["home_team"], row["away_team"])
            if wr is None:
                continue
            hs, as_ = wr
        log.loc[i, "actual_home_score"] = hs
        log.loc[i, "actual_away_score"] = as_
        log.loc[i, "actual_margin"] = hs - as_
        n_filled += 1

    log.to_csv(LOG_PATH, index=False)
    print(f"Filled in {n_filled} newly-resolved result(s). {log['actual_home_score'].isna().sum()} still unresolved.")

    resolved = log[log["actual_margin"].notna()].copy()
    if resolved.empty:
        print("No resolved matches yet -- nothing to calibrate against.")
        return

    print(f"\n=== Calibration summary ({len(resolved)} resolved matches) ===")
    pred_mae = (resolved["pred_margin"] - resolved["actual_margin"]).abs().mean()
    print(f"Model-margin MAE:            {pred_mae:.2f} pts")

    with_market = resolved.dropna(subset=["market_margin"]).copy()
    if len(with_market):
        act = with_market["actual_margin"]
        market_mae = (with_market["market_margin"] - act).abs().mean()
        model_mae = (with_market["pred_margin"] - act).abs().mean()
        blend_mae = (with_market["blended_margin"] - act).abs().mean()
        print(f"On the {len(with_market)} matches with a logged market line:")
        print(f"  Market-line MAE:           {market_mae:.2f} pts")
        print(f"  Model-margin MAE:          {model_mae:.2f} pts")
        print(f"  Blended-margin MAE:        {blend_mae:.2f} pts  (as logged)")
        bias = (with_market["pred_margin"] - with_market["market_margin"]).mean()
        print(f"  Avg (model - market):     {bias:+.2f} pts "
              f"({'model more conservative' if bias < 0 else 'model more bullish on home'})")

        # ex-post: which blend weight would have been best so far?
        if len(with_market) >= 6:
            weights = np.linspace(0, 1, 21)
            maes = [((w * with_market["pred_margin"] + (1 - w) * with_market["market_margin"] - act)
                     .abs().mean()) for w in weights]
            best_w = weights[int(np.argmin(maes))]
            print(f"  Ex-post best model weight: {best_w:.2f}  (MAE {min(maes):.2f}); "
                  f"currently logging {with_market['model_weight'].dropna().iloc[-1]}")
    else:
        print("No rows have a logged --market-line yet -- nothing to compare against the market.")

    for col, label in [("pred_margin", "Model"), ("blended_margin", "Blended")]:
        sub = resolved.dropna(subset=[col])
        if len(sub):
            pick = ((sub[col] > 0) == (sub["actual_margin"] > 0)).mean()
            print(f"{label} home/away pick correct: {pick:.0%} (n={len(sub)})")


if __name__ == "__main__":
    main()
