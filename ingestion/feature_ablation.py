"""
Feature ablation for the international match predictor.

The shipped model (predict_upcoming_match.py) is deliberately LEAN:
    elo_diff, venue_loss_streak_home, venue_loss_streak_away
because as of Aug 2026 (~260 training rows) every richer feature set tested
flat-to-worse on held-out data. Those null results are a sample-size ceiling,
not proof the features are useless -- the tracked-vs-tracked set grows ~40-50
rows/year, so this is meant to be re-run periodically.

    python ingestion/feature_ablation.py            # all groups, 3 splits
    python ingestion/feature_ablation.py --splits 2024-01-01 2025-01-01

For each split it fits, on matches before the split date:
  - Ridge (alpha=5) on the margin  -> MAE on matches after
  - LogisticRegression + shallow XGBClassifier on home-win -> AUC / logloss / Brier
and prints LEAN vs LEAN+group vs FULL. A group "helps" only if it beats LEAN
on the headline metrics on a majority of splits.
"""

import argparse
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.metrics import brier_score_loss, log_loss, mean_absolute_error, roc_auc_score
from sklearn.preprocessing import StandardScaler
from xgboost import XGBClassifier

DATA = Path(__file__).resolve().parent.parent / "data" / "processed" / "intl_match_features.csv"
DEFAULT_SPLITS = ["2023-09-01", "2024-01-01", "2024-09-01"]

LEAN = ["elo_diff", "venue_loss_streak_home", "venue_loss_streak_away"]

# candidate feature families layered on top of LEAN
GROUPS = {
    "form": ["form5_home", "form5_away", "pdiff5_home", "pdiff5_away"],
    "venue_form": ["venue_form5_home", "venue_form5_away"],
    "vs_stronger": ["form_vs_stronger_home", "form_vs_stronger_away"],
    "rest": ["rest_days_home", "rest_days_away"],
    "rank": ["rank_prev_home", "rank_prev_away"],
    "coach_tenure": ["coach_tenure_home", "coach_tenure_away"],
    "squad_age": ["avg_age_home", "avg_age_away"],
    "combo": ["combo_avg_home", "combo_avg_away", "combo_min_home", "combo_min_away"],
    "xv_cohesion": ["xv_cohesion_avg_home", "xv_cohesion_avg_away",
                    "xv_cohesion_min_home", "xv_cohesion_min_away"],
    "sq23_cohesion": ["sq23_cohesion_avg_home", "sq23_cohesion_avg_away"],
    "xv_retained": ["xv_retained_home", "xv_retained_away"],
    "xv_starts": ["xv_starts_avg_home", "xv_starts_avg_away"],
    "cards": ["cards5_home", "cards5_away"],
    "weather": ["weather_temp_c", "weather_precip_mm", "weather_wind_kmh"],
    "neutral": ["neutral"],
    "rematch": ["rm_margin", "rm_resid", "rm_flag"],   # computed below, not dataset columns
}
FULL = LEAN + [f for g in GROUPS.values() for f in g if not f.startswith("rm_")]


def add_rematch_features(df: pd.DataFrame, window_days: int = 45) -> pd.DataFrame:
    """Prior-meeting signal: if the two teams met within `window_days`, the current
    home team's margin / Elo-residual in that meeting (0 if no recent meeting)."""
    df = df.sort_values("date").reset_index(drop=True)
    last: dict = {}
    rm_margin = np.zeros(len(df))
    rm_resid = np.zeros(len(df))
    rm_flag = np.zeros(len(df))
    for i, r in df.iterrows():
        key = frozenset((r.home_team, r.away_team))
        prev = last.get(key)
        if prev is not None and (r.date - prev["date"]).days <= window_days:
            sign = 1.0 if prev["home_team"] == r.home_team else -1.0
            rm_margin[i] = sign * prev["margin"]
            rm_resid[i] = sign * prev["margin"] - sign * prev["elo_diff"] / 25.0
            rm_flag[i] = 1.0
        last[key] = {"date": r.date, "home_team": r.home_team,
                     "margin": r.margin, "elo_diff": r.elo_diff}
    df["rm_margin"], df["rm_resid"], df["rm_flag"] = rm_margin, rm_resid, rm_flag
    return df


def evaluate(df: pd.DataFrame, feats: list[str], split: str) -> dict:
    tr, te = df[df.date < split], df[df.date >= split]
    Xtr, Xte = tr[feats].copy(), te[feats].copy()
    med = Xtr.median()
    Xtr, Xte = Xtr.fillna(med).fillna(0), Xte.fillna(med).fillna(0)
    ytr, yte = tr.home_win, te.home_win
    mtr, mte = tr.margin, te.margin

    ridge = Ridge(alpha=5.0).fit(Xtr, mtr)
    mae = mean_absolute_error(mte, ridge.predict(Xte))

    sc = StandardScaler().fit(Xtr)
    lr = LogisticRegression(max_iter=2000).fit(sc.transform(Xtr), ytr)
    lp = lr.predict_proba(sc.transform(Xte))[:, 1]

    xgb = XGBClassifier(n_estimators=100, max_depth=2, learning_rate=0.05, subsample=0.8,
                        colsample_bytree=0.8, reg_lambda=2.0, eval_metric="logloss").fit(Xtr, ytr)
    xp = xgb.predict_proba(Xte)[:, 1]

    return {
        "n_test": len(te),
        "margin_mae": mae,
        "lr_auc": roc_auc_score(yte, lp), "lr_logloss": log_loss(yte, lp),
        "xgb_auc": roc_auc_score(yte, xp), "xgb_logloss": log_loss(yte, xp),
        "xgb_brier": brier_score_loss(yte, xp),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--splits", nargs="+", default=DEFAULT_SPLITS)
    args = ap.parse_args()

    df = pd.read_csv(DATA, parse_dates=["date"]).dropna(subset=["elo_diff"])
    df["home_win"] = (df["result"] == "w").astype(int)
    df["margin"] = df["home_score"] - df["away_score"]
    df = add_rematch_features(df)
    print(f"{len(df)} matches, {df.date.min().date()}..{df.date.max().date()}  |  splits: {args.splits}\n")

    configs = {"LEAN": LEAN, "FULL": FULL}
    for name, extra in GROUPS.items():
        configs[f"LEAN+{name}"] = LEAN + extra

    lean_avg = {}
    rows = []
    for name, feats in configs.items():
        per = [evaluate(df, feats, s) for s in args.splits]
        avg = {k: float(np.mean([p[k] for p in per])) for k in per[0] if k != "n_test"}
        rows.append((name, avg))
        if name == "LEAN":
            lean_avg = avg

    hdr = f"{'config':22s} {'marginMAE':>10} {'xgb_AUC':>9} {'xgb_LL':>8} {'xgb_Brier':>10} {'LR_AUC':>8} {'LR_LL':>8}"
    print(hdr)
    print("-" * len(hdr))
    for name, a in rows:
        better = (a["xgb_auc"] > lean_avg["xgb_auc"] + 0.003
                  and a["xgb_logloss"] < lean_avg["xgb_logloss"] - 0.003
                  and a["margin_mae"] < lean_avg["margin_mae"] - 0.05) if name not in ("LEAN",) else False
        mark = "  <-- beats LEAN" if better else ""
        print(f"{name:22s} {a['margin_mae']:10.2f} {a['xgb_auc']:9.3f} {a['xgb_logloss']:8.3f} "
              f"{a['xgb_brier']:10.3f} {a['lr_auc']:8.3f} {a['lr_logloss']:8.3f}{mark}")

    print("\nA group only earns a place if it beats LEAN on AUC + logloss + margin MAE "
          "(small thresholds) averaged across all splits. As of Aug 2026 none do.")


if __name__ == "__main__":
    main()
