# Shared by memsync.sh and setup.sh. Sourced, not run.
#
# The rules everything here follows:
#   1. Never fail or hang the session. Hooks always exit 0, git never prompts,
#      network calls give up instead of waiting forever.
#   2. Never leave conflict markers in a file. Claude reads memory as fact, so a
#      file full of <<<<<<< lines is worse than a stale one.
#   3. Never lose an edit. When two machines change the same thing, both
#      versions are kept and listed for tidying.

export GIT_TERMINAL_PROMPT=0 GIT_MERGE_AUTOEDIT=no
export GIT_HTTP_LOW_SPEED_LIMIT=1000 GIT_HTTP_LOW_SPEED_TIME=15
export GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh -o BatchMode=yes -o ConnectTimeout=10}"

MEMSYNC_SCRIPTS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST="${MEMSYNC_HOST:-$( (scutil --get LocalHostName 2>/dev/null || hostname -s 2>/dev/null || hostname) | tr -d '\r\n ')}"

# Lines that look like a credential. A file whose new lines match is held back
# from the commit, so a pasted token never reaches the remote.
SECRET_RE='sk-ant-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{32,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[0-9A-Z]{16}|xox[abpr]-[A-Za-z0-9-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY-----'

# Which memory folder to sync: an explicit override, the plugin option, or the
# memory folder Claude Code keeps for the current project.
memory_dir() {
  if [ -n "${MEMSYNC_DIR:-}" ]; then printf '%s' "$MEMSYNC_DIR"; return; fi
  if [ -n "${CLAUDE_PLUGIN_OPTION_MEMORY_DIR:-}" ]; then printf '%s' "${CLAUDE_PLUGIN_OPTION_MEMORY_DIR/#\~/$HOME}"; return; fi
  local proj="${CLAUDE_PROJECT_DIR:-$PWD}"
  printf '%s/.claude/projects/%s/memory' "$HOME" "$(printf '%s' "$proj" | sed 's/[^A-Za-z0-9]/-/g')"
}

# Per-machine state lives inside .git, so it is never committed or synced.
state_init() {
  GITDIR="$(git rev-parse --absolute-git-dir 2>/dev/null)" || return 1
  STATE="$GITDIR/memsync"; mkdir -p "$STATE"
  LOG="$STATE/log"; TIDY="$STATE/tidy"; HELD="$STATE/held"
}

log() { printf '%s [%s] %s\n' "$(date '+%Y-%m-%dT%H:%M:%S')" "${MODE:-setup}" "$*" >> "$LOG"; }

# The merge driver keeps both sides of overlapping edits in .md files. It is
# re-registered on every run so it follows the scripts if they move (a plugin
# update changes their path).
configure_driver() {
  git config merge.memsync.name "memsync: keep both sides of overlapping memory edits"
  git config merge.memsync.driver "\"$MEMSYNC_SCRIPTS/merge-driver.sh\" %O %A %B %P"
}

# Commit whatever changed here, holding back files that look like they
# contain a credential.
commit_local() {
  : > "$HELD"
  [ -z "$(git status --porcelain)" ] && return 0
  git add -A
  local f
  while IFS= read -r -d '' f; do
    if git diff --cached -U0 -- "$f" | grep '^+' | grep -Eq -- "$SECRET_RE"; then
      git reset -q -- "$f"; printf '%s\n' "$f" >> "$HELD"
    fi
  done < <(git diff --cached --name-only -z --diff-filter=AM)
  if ! git diff --cached --quiet; then
    local n; n=$(git diff --cached --name-only | wc -l | tr -d ' ')
    git commit -q -m "memory: $n file(s) from $HOST $(date -u +%Y-%m-%dT%H:%MZ)" >>"$LOG" 2>&1 && log "committed $n file(s)"
  fi
  [ -s "$HELD" ] && log "held back (looks like a credential): $(tr '\n' ' ' < "$HELD")"
  return 0
}

conflict_name() {  # notes/a.md -> notes/a.conflict-HOST-STAMP.md
  local f=$1 s=$2 base ext
  base="${f%.*}"; ext="${f##*.}"
  if [ "$base" = "$f" ] || [ -z "$base" ] || [[ "$ext" == */* ]]; then
    printf '%s.conflict-%s-%s' "$f" "$HOST" "$s"
  else
    printf '%s.conflict-%s-%s.%s' "$base" "$HOST" "$s" "$ext"
  fi
}

# What the driver cannot settle (a file deleted on one machine and changed on
# the other, or a non-text file changed on both): keep both versions as files.
resolve_with_copies() {
  local stamp f copy; stamp=$(date +%Y%m%d-%H%M)
  while IFS= read -r -d '' f; do
    local ours theirs
    ours=$(git ls-files -u -- "$f" | awk '$3==2' | head -1)
    theirs=$(git ls-files -u -- "$f" | awk '$3==3' | head -1)
    if [ -n "$theirs" ]; then
      if [ -n "$ours" ]; then
        copy=$(conflict_name "$f" "$stamp")
        git show ":2:$f" > "$copy" && git add -- "$copy"
        printf 'copy\t%s\tchanged on two machines: the other machine'"'"'s version is in place, this machine'"'"'s is in %s\n' "$f" "$copy" >> "$TIDY"
      else
        printf 'restored\t%s\tdeleted here but changed on another machine: kept the changed version\n' "$f" >> "$TIDY"
      fi
      git checkout -q --theirs -- "$f" && git add -- "$f"
    else
      git checkout -q --ours -- "$f" && git add -- "$f"
      printf 'kept\t%s\tdeleted on another machine but changed here: kept the changed version\n' "$f" >> "$TIDY"
    fi
  done < <(git diff --name-only -z --diff-filter=U)
}

# Bring the remote's commits in. Fast-forward when possible, otherwise replay
# ours on top, otherwise one merge with conflict copies. Returns 1 only if
# none of that worked, and then the repo is left exactly as it was.
integrate() {
  local up head base
  up=$(git rev-parse -q --verify '@{u}' 2>/dev/null) || return 0
  head=$(git rev-parse HEAD)
  [ "$head" = "$up" ] && return 0
  base=$(git merge-base HEAD "$up" 2>/dev/null || true)
  [ "$base" = "$up" ] && return 0                          # only ahead: just push
  if [ "$base" = "$head" ]; then
    git merge -q --ff-only "$up" >>"$LOG" 2>&1 && { log "fast-forwarded"; return 0; }
  fi
  local unrelated=""; [ -z "$base" ] && unrelated="--allow-unrelated-histories"
  if [ -n "$base" ] && git rebase -q --autostash "$up" >>"$LOG" 2>&1; then
    log "rebased onto remote"; return 0
  fi
  git rebase --abort >>"$LOG" 2>&1 || true
  if git merge -q --no-edit --autostash $unrelated "$up" >>"$LOG" 2>&1; then
    log "merged remote"; return 0
  fi
  resolve_with_copies
  if [ -z "$(git diff --name-only --diff-filter=U)" ] && git commit -q --no-edit >>"$LOG" 2>&1; then
    log "merged remote, keeping both versions of conflicting files"; return 0
  fi
  git merge --abort >>"$LOG" 2>&1 || true
  log "could not bring in the remote; local memory left as it was"
  return 1
}

# Push, and if another machine pushed in the meantime, bring that in and retry.
push_out() {
  local i ahead
  for i in 1 2 3; do
    ahead=$(git rev-list --count '@{u}..HEAD' 2>/dev/null || echo 0)
    [ "$ahead" -eq 0 ] && return 0
    if git push -q 2>>"$LOG"; then log "pushed $(git rev-parse --short HEAD)"; return 0; fi
    git fetch -q origin 2>>"$LOG" && integrate || return 1
  done
  return 1
}

json_escape() {
  local s=$1
  s=${s//\\/\\\\}; s=${s//\"/\\\"}; s=${s//$'\t'/\\t}; s=${s//$'\n'/\\n}; s=${s//$'\r'/}
  printf '%s' "$s"
}
