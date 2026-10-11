#!/usr/bin/env bash
# Hand the daily run to a fresh runner, then stop this one (.github/workflows/daily.yml).
#
#   pipeline/hand-off-day.sh "<why this host cannot embed>"
#
# Reads ATTEMPT, SPEND, WEEKS and GH_TOKEN from the job. The new run gets the same spend and revisit inputs
# and the next try number; on the sixth try nothing is handed on and the day is skipped, so the next schedule
# tries again. Called before anything is bought, so a handed-off run costs nothing.
set -euo pipefail
why="$1"
if [ "$ATTEMPT" -ge 6 ]; then
  echo "::warning::$why on try $ATTEMPT, the last; skipping the day"
else
  echo "::notice::$why; handing the day to try $((ATTEMPT + 1))"
  gh workflow run daily.yml -R "$GITHUB_REPOSITORY" --ref main \
    -f spend="$SPEND" -f revisit_weeks="$WEEKS" -f host_attempt="$((ATTEMPT + 1))"
fi
gh run cancel "$GITHUB_RUN_ID" -R "$GITHUB_REPOSITORY"
sleep 300
exit 1
