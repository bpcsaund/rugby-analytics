"""
Sanity checks run in CI after rebuilding the international match dataset.
Catches structural regressions (e.g. the float("nan") bug that silently let
blank scores corrupt every downstream Elo rating) without needing a specific
expected value to assert against.
"""

import sys

import pandas as pd

PATH = "data/processed/intl_match_features.csv"
MIN_ROWS = 350   # current count is 387; alert well before a real drop


def main():
    df = pd.read_csv(PATH)
    failures = []

    if len(df) < MIN_ROWS:
        failures.append(f"only {len(df)} rows, expected >= {MIN_ROWS}")

    for col in ["elo_home", "elo_away", "elo_diff", "home_score", "away_score"]:
        n_nan = df[col].isna().sum()
        if n_nan:
            failures.append(f"{col} has {n_nan} NaN values (expected 0)")

    if not df["elo_home"].between(1000, 2500).all():
        failures.append("elo_home has values outside the sane 1000-2500 range")

    valid_results = {"w", "l", "d"}
    bad_results = set(df["result"].unique()) - valid_results
    if bad_results:
        failures.append(f"unexpected result values: {bad_results}")

    if (df["home_score"] < 0).any() or (df["away_score"] < 0).any():
        failures.append("negative score(s) found")

    if failures:
        print("CI SANITY CHECK FAILURES:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)

    print(f"All sanity checks passed ({len(df)} rows).")


if __name__ == "__main__":
    main()
