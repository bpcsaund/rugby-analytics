"""
Fills in actual results for rows logged by predict_upcoming_match.py --log,
then reports how our projected margin compares to the betting market's line
(where logged) once results are known.

Run this periodically (e.g. after each weekend's Tests) once fixtures logged
via --log have been played.
"""

from pathlib import Path

import numpy as np
import pandas as pd

from build_intl_match_dataset import build_rows_and_state

LOG_PATH = Path(__file__).resolve().parent.parent / "data" / "processed" / "calibration_log.csv"


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
        if match.empty:
            continue
        m = match.iloc[0]
        log.loc[i, "actual_home_score"] = m["home_score"]
        log.loc[i, "actual_away_score"] = m["away_score"]
        log.loc[i, "actual_margin"] = m["home_score"] - m["away_score"]
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
