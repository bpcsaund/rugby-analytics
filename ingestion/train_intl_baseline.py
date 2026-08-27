"""
Baseline international match-outcome models trained on
data/processed/intl_match_features.csv (see build_intl_match_dataset.py).

Predicts home win vs. not (draws folded into "not", since there are only 7 in
387 rows -- too few to model as a third class). Evaluated with a time-based
train/test split (train on matches before TEST_START, test after) rather than
random k-fold, since these matches are not i.i.d. over time (Elo/form carry
forward, and a random split would leak future information into training).
"""

from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, brier_score_loss, log_loss, roc_auc_score
from sklearn.preprocessing import StandardScaler
from xgboost import XGBClassifier

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "processed" / "intl_match_features.csv"
TEST_START = "2024-01-01"

# Walk-forward backtests over 2023-26 (and 3 different split dates) repeatedly
# showed Elo alone matching or beating the full feature set, with only the
# venue loss-streak adding consistent signal on top. The production predictor
# (predict_upcoming_match.py) therefore ships LEAN; FEATURES is kept here so the
# backtest keeps showing what the wider feature set is (not) contributing.
LEAN = ["elo_diff", "venue_loss_streak_home", "venue_loss_streak_away"]
FEATURES = [
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


def load():
    df = pd.read_csv(DATA_PATH, parse_dates=["date"])
    df = df.dropna(subset=["elo_diff"]).sort_values("date").reset_index(drop=True)
    df["home_win"] = (df["result"] == "w").astype(int)
    return df


def report(name, y_true, y_pred, y_prob):
    print(f"\n{name}")
    print(f"  accuracy   : {accuracy_score(y_true, y_pred):.3f}")
    print(f"  auc        : {roc_auc_score(y_true, y_prob):.3f}")
    print(f"  log_loss   : {log_loss(y_true, y_prob):.3f}")
    print(f"  brier      : {brier_score_loss(y_true, y_prob):.3f}")


def main():
    df = load()
    train = df[df["date"] < TEST_START]
    test = df[df["date"] >= TEST_START]
    print(f"Train: {len(train)} matches ({train['date'].min().date()} .. {train['date'].max().date()})")
    print(f"Test:  {len(test)} matches ({test['date'].min().date()} .. {test['date'].max().date()})")
    print(f"Test-set home-win base rate: {test['home_win'].mean():.3f}")

    X_train, y_train = train[FEATURES], train["home_win"]
    X_test, y_test = test[FEATURES], test["home_win"]

    # median-impute from train only, to avoid leaking test-period stats
    medians = X_train.median()
    X_train = X_train.fillna(medians)
    X_test = X_test.fillna(medians)

    # --- Elo-only sanity baseline: no fitting, just "favorite by Elo wins" ---
    elo_only_pred = (X_test["elo_diff"] > 0).astype(int)
    elo_only_prob = 1 / (1 + 10 ** (-X_test["elo_diff"] / 400))  # classic Elo win prob, no fitting
    report("Elo-implied win probability (no ML, sanity baseline)", y_test, elo_only_pred, elo_only_prob)

    # --- Logistic regression (scaled -- features are on very different scales) ---
    scaler = StandardScaler().fit(X_train)
    X_train_s = scaler.transform(X_train)
    X_test_s = scaler.transform(X_test)
    logreg = LogisticRegression(max_iter=1000, C=1.0)
    logreg.fit(X_train_s, y_train)
    lr_prob = logreg.predict_proba(X_test_s)[:, 1]
    lr_pred = (lr_prob >= 0.5).astype(int)
    report("Logistic Regression", y_test, lr_pred, lr_prob)
    coefs = pd.Series(logreg.coef_[0], index=FEATURES).sort_values(key=abs, ascending=False)
    print("  top coefficients:")
    for feat, val in coefs.head(6).items():
        print(f"    {feat:20s} {val:+.4f}")

    # --- XGBoost, deliberately shallow/regularized given ~330 training rows ---
    xgb = XGBClassifier(
        n_estimators=100, max_depth=2, learning_rate=0.05,
        subsample=0.8, colsample_bytree=0.8, reg_lambda=2.0,
        eval_metric="logloss",
    )
    xgb.fit(X_train, y_train)
    xgb_prob = xgb.predict_proba(X_test)[:, 1]
    xgb_pred = (xgb_prob >= 0.5).astype(int)
    report("XGBoost (shallow, regularized)", y_test, xgb_pred, xgb_prob)
    importances = pd.Series(xgb.feature_importances_, index=FEATURES).sort_values(ascending=False)
    print("  top feature importances:")
    for feat, val in importances.head(6).items():
        print(f"    {feat:20s} {val:.3f}")

    # --- always-predict-home-favorite-by-scoreline-history baseline ---
    majority_prob = np.full(len(y_test), y_train.mean())
    majority_pred = np.full(len(y_test), int(y_train.mean() >= 0.5))
    report("Majority-class baseline (predict train home-win rate)", y_test, majority_pred, majority_prob)

    # --- LEAN feature set (what the production predictor actually ships) ---
    Xl_train = train[LEAN].fillna(train[LEAN].median())
    Xl_test = test[LEAN].fillna(train[LEAN].median())
    lean_xgb = XGBClassifier(n_estimators=100, max_depth=2, learning_rate=0.05,
                              subsample=0.8, colsample_bytree=0.8, reg_lambda=2.0,
                              eval_metric="logloss").fit(Xl_train, y_train)
    lean_prob = lean_xgb.predict_proba(Xl_test)[:, 1]
    report(f"XGBoost on LEAN {LEAN}", y_test, (lean_prob >= 0.5).astype(int), lean_prob)


if __name__ == "__main__":
    main()
