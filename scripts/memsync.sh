#!/usr/bin/env bash
# memsync.sh: keep a Claude Code memory folder in step with a private git repo.
#
#   memsync.sh pull     session start: commit local edits, bring in the remote, push
#   memsync.sh push     after each reply and at session end: the same, quietly
#   memsync.sh status   ahead/behind, last good sync, anything waiting to be tidied
#   memsync.sh tidied   clear the tidy list once the listed files have been checked
#
# Add --dir DIR to any of them to name the memory folder directly.
set -u
MODE="${1:-push}"
[ "${2:-}" = "--dir" ] && [ -n "${3:-}" ] && export MEMSYNC_DIR="$3"
source "$(cd "$(dirname "$0")" && pwd)/lib.sh"

DIR="$(memory_dir)"
if [ ! -d "$DIR/.git" ] || [ ! -f "$DIR/.memsync" ]; then
  [ "$MODE" = status ] && echo "memsync: $DIR is not set up. Run setup.sh to turn syncing on."
  exit 0   # not set up for this project: stay out of the way
fi
cd "$DIR" || exit 0
state_init || exit 0

report() {  # only at session start, and only when there is something to act on
  [ "$MODE" = pull ] || return 0
  local ctx="" sys="" fails last
  if [ -s "$TIDY" ]; then
    ctx+="Memory sync merged edits made on two machines. Check each file below for duplicated or contradictory lines and keep the correct version. Fold any *.conflict-* copy into its original and delete the copy. Then run: \"$MEMSYNC_SCRIPTS/memsync.sh\" tidied --dir \"$DIR\""$'\n'
    ctx+="$(sort -u "$TIDY" | awk -F'\t' '{print "- " $2 ": " $3}')"$'\n'
  fi
  if [ -s "$HELD" ]; then
    ctx+="These memory files were NOT synced because they look like they contain a credential. Tell the user, and remove the secret so they can sync:"$'\n'
    ctx+="$(sed 's/^/- /' "$HELD")"$'\n'
  fi
  fails=$(cat "$STATE/fails" 2>/dev/null || echo 0)
  if [ "$fails" -ge 3 ]; then
    last=$(cat "$STATE/last_ok" 2>/dev/null || echo "")
    [ -n "$last" ] && last=$(date -r "$last" '+%d %b %H:%M' 2>/dev/null || date -d "@$last" '+%d %b %H:%M' 2>/dev/null)
    sys="Memory has not synced for $fails attempts (last good sync: ${last:-never}). This machine's memory is intact but may be missing other machines' changes. Details: \"$MEMSYNC_SCRIPTS/memsync.sh\" status --dir \"$DIR\""
    ctx+="$sys Mention this to the user."$'\n'
  fi
  [ -z "$ctx" ] && return 0
  if [ -n "$sys" ]; then
    printf '{"systemMessage":"%s","hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"%s"}}\n' "$(json_escape "$sys")" "$(json_escape "$ctx")"
  else
    printf '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"%s"}}\n' "$(json_escape "$ctx")"
  fi
}

status() {
  local ahead behind last fails
  git fetch -q origin 2>/dev/null
  ahead=$(git rev-list --count '@{u}..HEAD' 2>/dev/null || echo "?")
  behind=$(git rev-list --count 'HEAD..@{u}' 2>/dev/null || echo "?")
  last=$(cat "$STATE/last_ok" 2>/dev/null || echo "")
  [ -n "$last" ] && last=$(date -r "$last" '+%Y-%m-%d %H:%M' 2>/dev/null || date -d "@$last" '+%Y-%m-%d %H:%M' 2>/dev/null)
  fails=$(cat "$STATE/fails" 2>/dev/null || echo 0)
  echo "memsync: $DIR"
  echo "  remote:          $(git remote get-url origin 2>/dev/null || echo none)"
  echo "  not yet pushed:  $ahead commit(s); not yet pulled: $behind"
  echo "  last good sync:  ${last:-never}; failed attempts since: $fails"
  if [ -s "$TIDY" ]; then echo "  to tidy:"; sort -u "$TIDY" | awk -F'\t' '{print "    " $2 ": " $3}'; fi
  if [ -s "$HELD" ]; then echo "  held back (looks like a credential):"; sed 's/^/    /' "$HELD"; fi
  [ "$fails" -gt 0 ] && { echo "  recent log:"; tail -5 "$LOG" | sed 's/^/    /'; }
  return 0
}

case "$MODE" in
  status) status; exit 0 ;;
  tidied) : > "$TIDY"; echo "memsync: tidy list cleared"; exit 0 ;;
  pull|push) ;;
  *) echo "usage: memsync.sh pull|push|status|tidied [--dir DIR]" >&2; exit 0 ;;
esac

# One sync at a time per memory folder, across every session on this machine.
# Two syncs fetching at once was the original bug: they race on FETCH_HEAD and
# the remote-tracking ref, and one of them fails with a false "conflict".
LOCK="$STATE/lock"; waited=0; limit=20; [ "$MODE" = push ] && limit=40
while ! mkdir "$LOCK" 2>/dev/null; do
  if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +2 2>/dev/null)" ]; then rmdir "$LOCK" 2>/dev/null || rm -rf "$LOCK"; fi
  if [ "$waited" -ge "$limit" ]; then log "another sync held the lock for ${limit}s; skipped (the next one catches up)"; report; exit 0; fi
  sleep 1; waited=$((waited + 1))
done
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

configure_driver
# A sync killed half way leaves a rebase or merge open. Undo it: the commits
# from before it started are all still there.
if [ -d "$GITDIR/rebase-merge" ] || [ -d "$GITDIR/rebase-apply" ]; then git rebase --abort >>"$LOG" 2>&1; log "undid an unfinished rebase"; fi
if [ -f "$GITDIR/MERGE_HEAD" ]; then git merge --abort >>"$LOG" 2>&1; log "undid an unfinished merge"; fi

ok=1
commit_local
if git fetch -q origin 2>>"$LOG"; then
  integrate && push_out || ok=0
else
  log "fetch failed (offline or no access); will retry"; ok=0
fi
if [ "$ok" = 1 ]; then
  date +%s > "$STATE/last_ok"; echo 0 > "$STATE/fails"
else
  echo $(( $(cat "$STATE/fails" 2>/dev/null || echo 0) + 1 )) > "$STATE/fails"
fi
report
exit 0
