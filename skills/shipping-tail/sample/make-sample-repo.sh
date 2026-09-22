#!/usr/bin/env bash
# Build a throwaway repository with three features whose tails are known by
# construction, so the metrics can be checked against ground truth before
# anyone points them at a real codebase. Adoption dies at the setup step, so
# step one here is not "instrument everything first".
#
#   PROJ-1  clean          ships, then goes quiet          -> low tail mass
#   PROJ-2  drafty         ships early, heavy rework       -> high tail mass
#   PROJ-3  never settles  still moving at the window end  -> right-censored
#
# Usage:  bash make-sample-repo.sh [target-dir]
# Then:   python3 ../scripts/shipping_tail.py --repo <target-dir> \
#             --deploys <target-dir>/deploys.csv --quiet-days 7

set -euo pipefail

TARGET="${1:-$(mktemp -d -t shipping-tail-sample)}"
mkdir -p "$TARGET"
cd "$TARGET"

git init -q .
git config user.email sample@example.invalid
git config user.name "Sample Author"
git config commit.gpgsign false

# Fixed dates: the metrics are time-based, so a reproducible clock is the
# difference between a demo and a coin flip.
BASE_Y=2026
commit_at() { # commit_at <day-of-march> <subject> <path> <lines>
  local day="$1" subject="$2" path="$3" lines="$4"
  mkdir -p "$(dirname "$path")"
  # Append rather than overwrite so churn accumulates like real edits.
  for _ in $(seq 1 "$lines"); do echo "line $RANDOM" >>"$path"; done
  git add -A
  local stamp
  stamp=$(printf '%s-03-%02dT12:00:00+00:00' "$BASE_Y" "$day")
  GIT_AUTHOR_DATE="$stamp" GIT_COMMITTER_DATE="$stamp" \
    git commit -q -m "$subject"
}

# --- PROJ-1: builds for a while, ships, goes quiet -------------------------
commit_at 1  "PROJ-1: scaffold checkout contract"      src/checkout/api.ts 60
commit_at 2  "PROJ-1: implement checkout handler"      src/checkout/handler.ts 120
commit_at 3  "PROJ-1: checkout tests"                  src/checkout/handler.test.ts 90
commit_at 5  "PROJ-1: wire checkout route"             src/checkout/route.ts 30
commit_at 9  "PROJ-1: copy tweak after launch"         src/checkout/handler.ts 6

# --- PROJ-2: ships thin, then reworks for weeks ---------------------------
commit_at 4  "PROJ-2: quick search endpoint"           src/search/index.ts 40
commit_at 7  "PROJ-2: fix search pagination"           src/search/index.ts 70
commit_at 8  "PROJ-2: fix search ranking"              src/search/rank.ts 110
commit_at 11 "PROJ-2: fix search timeouts"             src/search/index.ts 80
commit_at 13 "PROJ-2: rewrite search ranking"          src/search/rank.ts 160
commit_at 15 "PROJ-2: search hotfix for empty query"   src/search/index.ts 25

# --- PROJ-3: still moving when the window closes --------------------------
commit_at 6  "PROJ-3: notifications skeleton"          src/notify/queue.ts 55
commit_at 10 "PROJ-3: notifications retry"             src/notify/queue.ts 45
commit_at 14 "PROJ-3: notifications backoff"           src/notify/backoff.ts 65
commit_at 17 "PROJ-3: notifications dedupe"            src/notify/dedupe.ts 70
commit_at 19 "PROJ-3: notifications metric"            src/notify/queue.ts 35
commit_at 20 "PROJ-3: notifications ordering bug"      src/notify/queue.ts 50

# Infra churn: real effort, but it belongs to no feature and must not inflate
# any cluster's tail.
commit_at 12 "ci: cache node modules"                  .github/workflows/ci.yml 20

# t=0 events. Flag activations where a team has them; these stand in for the
# moment each feature first served a user.
cat >deploys.csv <<'CSV'
# iso8601 timestamp, hint matched against cluster key or commit subjects
2026-03-06T09:00:00+00:00,proj-1
2026-03-05T09:00:00+00:00,proj-2
2026-03-07T09:00:00+00:00,proj-3
CSV

git add -A
GIT_AUTHOR_DATE="2026-03-20T13:00:00+00:00" GIT_COMMITTER_DATE="2026-03-20T13:00:00+00:00" \
  git commit -q -m "chore: record deploy events"

echo "$TARGET"
