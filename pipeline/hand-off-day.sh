#!/usr/bin/env bash
# Hand the daily run to a fresh runner, then stop this one (.github/workflows/daily.yml).
#
#   pipeline/hand-off-day.sh "<why this run cannot finish the day>" ["<cpu model, when it is not on the blocklist>"]
#
# Reads ATTEMPT, SPEND, WEEKS, WEEKLY_NOW and GH_TOKEN from the job. The new run gets the same spend, revisit
# and weekly-now inputs
# and the next try number; on the sixth try nothing is handed on and the day is skipped, so the next schedule
# tries again. Called before anything is bought (a CPU outside the box's space), or after the day stopped on a
# service that did not answer, once the cache and the paid-answers ledger are kept, so the next run resumes.
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
  -f spend="$SPEND" -f revisit_weeks="$WEEKS" -f weekly_now="${WEEKLY_NOW:-false}" -f host_attempt="$((ATTEMPT + 1))"
gh run cancel "$GITHUB_RUN_ID" -R "$GITHUB_REPOSITORY"
sleep 300
exit 1
