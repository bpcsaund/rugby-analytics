#!/bin/bash
# Weekly reconcile job: fills in results in the calibration log, prints a
# calibration summary, and commits/pushes the log if it changed.
#
# Scheduled via launchd (Mondays 09:00 local). The live copy of the plist is
# ~/Library/LaunchAgents/com.rugbyanalytics.weeklyreconcile.plist; a reference
# copy is committed at ops/com.rugbyanalytics.weeklyreconcile.plist.
#
# Runs headless: git is /usr/bin/git, GitLab push uses the passphrase-less
# ~/.ssh/id_ed25519, GitHub push uses the osxkeychain credential helper.

set -euo pipefail

REPO_DIR="$HOME/rugby_analytics"
LOG_FILE="$REPO_DIR/data/processed/reconcile_run.log"
CSV_PATH="data/processed/calibration_log.csv"

cd "$REPO_DIR"

{
  echo "=== Run at $(date) ==="
  ./.venv/bin/python ingestion/reconcile_calibration_log.py

  if git diff --quiet -- "$CSV_PATH"; then
    echo "No changes to calibration log; nothing to commit."
  else
    git add "$CSV_PATH"
    git commit -m "Weekly reconcile: $(date +%Y-%m-%d)"

    # Push to both remotes; don't let one failure skip the other, but do
    # surface a non-zero exit so launchd's error log flags it.
    push_failed=0
    git push github main || push_failed=1
    git push origin main || push_failed=1
    if [ "$push_failed" -eq 0 ]; then
      echo "Committed and pushed updated calibration log to both remotes."
    else
      echo "WARNING: commit made but at least one push failed - re-push manually."
      exit 1
    fi
  fi
  echo
} >> "$LOG_FILE" 2>&1
