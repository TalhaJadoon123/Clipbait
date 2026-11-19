#!/usr/bin/env bash
# One unattended earning pass: find the biggest live room, watch it, then
# package everything worth posting.
#
#   ./scripts/earn.sh                 # default query, 30 minutes, top 4 clips
#   QUERY="kai cenat" MINUTES=45 MAX_CLIPS=6 ./scripts/earn.sh
#   SOURCE="https://youtube.com/watch?v=..." ./scripts/earn.sh   # skip discovery
#   CAMPAIGN=data/campaigns/clipmoney.md ./scripts/earn.sh       # check the real brief
#
# The script never uploads. It leaves one folder per clip in data/exports/ with
# the video, caption, metadata and check result. Posting is a human action on
# the operator's own verified account.
set -euo pipefail

cd "$(dirname "$0")/.."

PY="${PY:-}"
if [ -z "$PY" ]; then
  if [ -x /tmp/opencode/clipbait-venv/bin/python ]; then
    PY=/tmp/opencode/clipbait-venv/bin/python
  else
    PY=python
  fi
fi

QUERY="${QUERY:-gta 6}"
MINUTES="${MINUTES:-30}"
MAX_CLIPS="${MAX_CLIPS:-4}"
MIN_SCORE="${MIN_SCORE:-35}"
THRESHOLD="${THRESHOLD:-3.0}"
CONTENT_THRESHOLD="${CONTENT_THRESHOLD:-1.9}"
PRESET="${PRESET:-hormozi}"
REGION="${REGION:-US}"
CAMPAIGN="${CAMPAIGN:-}"

if [ -z "${SOURCE:-}" ]; then
  echo "== finding live rooms for: $QUERY"
  JSON="$("$PY" -m clipbait.cli streams --query "$QUERY" --limit 10 --json)"
  SOURCE="$(printf '%s' "$JSON" | "$PY" -c 'import json,sys; rows=json.load(sys.stdin); print(rows[0]["url"] if rows else "")')"
  if [ -z "$SOURCE" ]; then
    echo "no live room found for '$QUERY'; try another query or hour" >&2
    exit 1
  fi
  printf '%s' "$JSON" | "$PY" -c '
import json, sys
for i, row in enumerate(json.load(sys.stdin), 1):
    print(f"  {i:2d}. {row[\"viewers\"]:>8,} viewers  {row[\"channel\"][:28]:28} {row[\"url\"]}")
'
fi

echo "== monitoring $SOURCE for ${MINUTES}m (max $MAX_CLIPS clips)"
set +e
timeout -s INT "$((MINUTES * 60))" \
  "$PY" -m clipbait.cli live "$SOURCE" \
    --title "Live capture" \
    --threshold "$THRESHOLD" \
    --content-threshold "$CONTENT_THRESHOLD" \
    --preset "$PRESET" \
    --region "$REGION" \
    --max-clips "$MAX_CLIPS" \
    --buffer-seconds 420
set -e

echo
echo "== what is ready to post"
if [ -n "$CAMPAIGN" ]; then
  "$PY" -m clipbait.cli queue --min-score "$MIN_SCORE" --export --campaign "$CAMPAIGN"
else
  "$PY" -m clipbait.cli queue --min-score "$MIN_SCORE" --export
  echo
  echo "NOTE: no CAMPAIGN set. The check used default sanity rules, not the brief."
  echo "      Paste the brief into data/campaigns/ (clipbait campaign) and re-run:"
  echo "      clipbait check <clip_id> --campaign data/campaigns/<file>.md"
fi

echo
echo "== files"
ls -1dt data/exports/*/ 2>/dev/null | head -"$MAX_CLIPS" || true