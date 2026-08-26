"""
Baseline scoreline (points, not just win/loss) regression on
data/processed/intl_match_features.csv. Same pre-match features and
time-based split as train_intl_baseline.py, but predicts home_score and
away_score directly rather than a win/loss class.

Rugby scorelines are high-variance (a converted try swings a margin by 7),
so don't expect tight MAE -- the point of this backtest is to see whether the
pre-match features beat "predict each team's recent scoring average" before
trusting a single-match projection.
"""

from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_error, mean_squared_error
from xgboost import XGBRegressor

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "processed" / "intl_match_features.csv"
TEST_START = "2024-01-01"

FEATURES = [
    "elo_diff", "form5_home", "form5_away", "pdiff5_home", "pdiff5_away",
    "rest_days_home", "rest_days_away", "rank_prev_home", "rank_prev_away",
    "coach_tenure_home", "coach_tenure_away", "avg_age_home", "avg_age_away",
    "combo_avg_home", "combo_avg_away", "combo_min_home", "combo_min_away",
    "cards5_home", "cards5_away",
]
TARGETS = ["home_score", "away_score"]


def report(name, y_true, y_pred):
    mae = mean_absolute_error(y_true, y_pred)
    rmse = np.sqrt(mean_squared_error(y_true, y_pred))
    print(f"  {name:38s} MAE={mae:5.2f}  RMSE={rmse:5.2f}")


def main():
    df = pd.read_csv(DATA_PATH, parse_dates=["date"]).dropna(subset=["elo_diff"]).sort_values("date")
    train = df[df["date"] < TEST_START]
    test = df[df["date"] >= TEST_START]
    print(f"Train: {len(train)} matches, Test: {len(test)} matches ({test['date'].min().date()}..{test['date'].max().date()})")

    X_train, X_test = train[FEATURES], test[FEATURES]
    medians = X_train.median()
    X_train = X_train.fillna(medians)
    X_test = X_test.fillna(medians)

    for target in TARGETS:
        print(f"\n=== {target} ===")
        y_train, y_test = train[target], test[target]

        naive_pred = np.full(len(y_test), y_train.mean())
        report("Naive (train mean)", y_test, naive_pred)

        ridge = Ridge(alpha=5.0).fit(X_train, y_train)
        report("Ridge regression", y_test, ridge.predict(X_test))

        xgb = XGBRegressor(n_estimators=100, max_depth=2, learning_rate=0.05,
                            subsample=0.8, colsample_bytree=0.8, reg_lambda=2.0)
        xgb.fit(X_train, y_train)
        report("XGBoost (shallow)", y_test, xgb.predict(X_test))

    print("\n=== margin (home_score - away_score) ===")
    train_margin = train["home_score"] - train["away_score"]
    test_margin = test["home_score"] - test["away_score"]
    naive_pred = np.full(len(test_margin), train_margin.mean())
    report("Naive (train mean margin)", test_margin, naive_pred)
    ridge = Ridge(alpha=5.0).fit(X_train, train_margin)
    report("Ridge regression", test_margin, ridge.predict(X_test))
    # margin derived from the elo-implied win prob as a sanity comparator
    elo_margin_est = X_test["elo_diff"] / 25   # ~25 elo pts per point is a common rough rugby conversion
    report("Elo-diff/25 heuristic", test_margin, elo_margin_est)


if __name__ == "__main__":
    main()
