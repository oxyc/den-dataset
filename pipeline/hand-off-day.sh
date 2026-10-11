#!/usr/bin/env bash
# Hand the daily run to a fresh runner, then stop this one (.github/workflows/daily.yml).
#
#   pipeline/hand-off-day.sh "<why this host cannot embed>" ["<cpu model, when it is not on the blocklist>"]
#
# Reads ATTEMPT, SPEND, WEEKS and GH_TOKEN from the job. The new run gets the same spend and revisit inputs
# and the next try number; on the sixth try nothing is handed on and the day is skipped, so the next schedule
# tries again. Called before anything is bought, so a handed-off run costs nothing.
#
# It leaves $RUNNER_TEMP/day-handoff (state=handed|exhausted, why=, cpu=) for pipeline/daily_issue.py, the
# workflow's last step, which reports an exhausted day as a failure and an unlisted CPU as an issue. A handed-off
# run is cancelled (it is not a failure: another run has the day); an exhausted one is left to fail, red.
set -euo pipefail
why="$1"
cpu="${2:-}"
state=handed
[ "$ATTEMPT" -ge 6 ] && state=exhausted
printf 'state=%s\nwhy=%s\ncpu=%s\n' "$state" "$why" "$cpu" > "${RUNNER_TEMP:-/tmp}/day-handoff"
if [ "$state" = exhausted ]; then
  echo "::warning::$why on try $ATTEMPT, the last; skipping the day"
  exit 1
fi
echo "::notice::$why; handing the day to try $((ATTEMPT + 1))"
gh workflow run daily.yml -R "$GITHUB_REPOSITORY" --ref main \
  -f spend="$SPEND" -f revisit_weeks="$WEEKS" -f host_attempt="$((ATTEMPT + 1))"
gh run cancel "$GITHUB_RUN_ID" -R "$GITHUB_REPOSITORY"
sleep 300
exit 1
