#!/usr/bin/env bash
# setup.sh: connect a Claude Code memory folder to a private git repo and turn
# syncing on. Run it once on each machine.
#
#   setup.sh [--dir DIR] [--repo NAME] [--remote URL] [--allow-public]
#
#   --dir      the memory folder (default: the one Claude Code keeps for the
#              project in the current directory)
#   --repo     name of the private GitHub repo to create or reuse (default:
#              agent-memory). Needs the GitHub CLI (gh), signed in.
#   --remote   use this git remote instead of GitHub
#
# On a second machine, point it at the same repo. Memory already on that
# machine is merged with the repo, not replaced.
set -u
REPO_NAME="agent-memory"; REMOTE=""; ALLOW_PUBLIC=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dir) export MEMSYNC_DIR="$2"; shift 2 ;;
    --repo) REPO_NAME="$2"; shift 2 ;;
    --remote) REMOTE="$2"; shift 2 ;;
    --allow-public) ALLOW_PUBLIC=1; shift ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "setup.sh: unknown option $1" >&2; exit 2 ;;
  esac
done
source "$(cd "$(dirname "$0")" && pwd)/lib.sh"
MODE=setup
die() { echo "setup.sh: $*" >&2; exit 1; }
command -v git >/dev/null || die "git is not installed"

DIR="$(memory_dir)"
mkdir -p "$DIR" && cd "$DIR" || die "cannot use $DIR"
echo "Memory folder: $DIR"

# Which remote to use.
if [ -d .git ] && git remote get-url origin >/dev/null 2>&1; then
  REMOTE="$(git remote get-url origin)"
elif [ -z "$REMOTE" ]; then
  command -v gh >/dev/null || die "the GitHub CLI (gh) is needed to create the repo. Install it, or pass --remote URL"
  gh auth status >/dev/null 2>&1 || die "gh is not signed in. Run: gh auth login"
  owner=$(gh api user -q .login) || die "could not read your GitHub user"
  if ! gh repo view "$owner/$REPO_NAME" >/dev/null 2>&1; then
    gh repo create "$owner/$REPO_NAME" --private --description "Agent memory, synced by Surya Memory" >/dev/null || die "could not create $owner/$REPO_NAME"
    echo "Created private repo $owner/$REPO_NAME"
  fi
  if [ "$(gh config get git_protocol 2>/dev/null)" = ssh ]; then
    REMOTE=$(gh repo view "$owner/$REPO_NAME" --json sshUrl -q .sshUrl)
  else
    REMOTE="$(gh repo view "$owner/$REPO_NAME" --json url -q .url).git"
    gh auth setup-git >/dev/null 2>&1 || true   # lets git push over https with gh's sign-in
  fi
fi
echo "Repo: $REMOTE"

# Memory is personal. Refuse a public GitHub repo unless told otherwise.
if [ "$ALLOW_PUBLIC" = 0 ] && command -v gh >/dev/null && [[ "$REMOTE" == *github.com* ]]; then
  slug=$(printf '%s' "$REMOTE" | sed -E 's#^(https://github.com/|git@github.com:)##; s#\.git$##')
  vis=$(gh repo view "$slug" --json visibility -q .visibility 2>/dev/null || echo "")
  [ "$vis" = PUBLIC ] && die "$slug is PUBLIC. Memory holds personal details, so use a private repo (or pass --allow-public)"
fi

if [ ! -d .git ]; then
  git init -q -b main . || die "git init failed"
fi
git remote get-url origin >/dev/null 2>&1 || git remote add origin "$REMOTE"
state_init || die "not a git repo"
[ -n "$(git config user.email)" ] || { git config user.name "$HOST"; git config user.email "memsync@$HOST.local"; }

[ -f .memsync ] || printf '%s\n' "This folder is synced by Surya Memory. Per-machine state is kept in .git/memsync." > .memsync
grep -qs 'merge=memsync' .gitattributes || printf '\n# Overlapping edits to memory files keep both sides (Surya Memory)\n*.md merge=memsync\n' >> .gitattributes
configure_driver

commit_local
if git fetch -q origin 2>>"$LOG" && git rev-parse -q --verify origin/main >/dev/null; then
  if ! git rev-parse -q --verify HEAD >/dev/null; then
    git checkout -q -B main origin/main || die "could not check out the repo"
  else
    git branch -q --set-upstream-to=origin/main main 2>/dev/null || true
    integrate || die "could not merge this machine's memory with the repo; nothing was changed"
  fi
  git branch -q --set-upstream-to=origin/main main 2>/dev/null || true
fi
if git rev-parse -q --verify HEAD >/dev/null; then
  git push -q -u origin main 2>>"$LOG" || die "push failed; check access to $REMOTE (log: $LOG)"
fi
date +%s > "$STATE/last_ok"; echo 0 > "$STATE/fails"

echo
echo "Memory sync is on for this folder. It syncs when a session starts and after each reply."
[ -s "$TIDY" ] && { echo "Files changed on both sides were merged by keeping both versions:"; sort -u "$TIDY" | awk -F'\t' '{print "  " $2}'; }
[ -s "$HELD" ] && { echo "Held back because they look like they contain a credential:"; sed 's/^/  /' "$HELD"; }
exit 0
