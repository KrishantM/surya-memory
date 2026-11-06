#!/usr/bin/env bash
# End-to-end tests: two simulated machines ("mac" and "win") sharing one bare
# remote, plus a third clone that behaves like a plain `git pull --rebase` user.
# Run: bash tests/run.sh   (macOS's /bin/bash 3.2 and newer bash both work)
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; S="$ROOT/scripts"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
export GIT_CONFIG_GLOBAL="$T/gitconfig" GIT_CONFIG_NOSYSTEM=1 HOME="$T/home"
mkdir -p "$HOME"
git config --global user.name tester; git config --global user.email tester@example.com
git config --global init.defaultBranch main
pass=0; fail=0
check() { if eval "$2"; then pass=$((pass + 1)); echo "ok    $1"; else fail=$((fail + 1)); echo "FAIL  $1"; fi; }
A="$T/a/memory"; B="$T/b/memory"; R="$T/remote.git"
sa() { MEMSYNC_HOST=mac MEMSYNC_DIR="$A" bash "$S/memsync.sh" "$@"; }
sb() { MEMSYNC_HOST=win MEMSYNC_DIR="$B" bash "$S/memsync.sh" "$@"; }
no_markers() { ! grep -rqs --exclude-dir=.git '^<<<<<<< ' "$A" "$B"; }
same() { [ "$(git -C "$A" rev-parse HEAD)" = "$(git -C "$B" rev-parse HEAD)" ] && [ "$(git -C "$A" rev-parse HEAD)" = "$(git -C "$R" rev-parse main)" ]; }
tidy_has() { grep -qs "$2" "$1/.git/memsync/tidy"; }

git init -q --bare -b main "$R"

echo "## setup"
mkdir -p "$A"; printf '# Index\n- one\n' > "$A/MEMORY.md"; printf 'fact one\n' > "$A/one.md"; printf '{"a":1}\n' > "$A/settings.json"
MEMSYNC_HOST=mac bash "$S/setup.sh" --dir "$A" --remote "$R" > "$T/setup-a.out" 2>&1
check "first machine pushes its memory" '[ "$(git -C "$R" rev-parse main)" = "$(git -C "$A" rev-parse HEAD)" ]'
mkdir -p "$B"; printf '# Win index\n- win note\n' > "$B/MEMORY.md"; printf 'win only\n' > "$B/win.md"
MEMSYNC_HOST=win bash "$S/setup.sh" --dir "$B" --remote "$R" > "$T/setup-b.out" 2>&1
check "second machine keeps its own files" '[ -f "$B/win.md" ] && [ -f "$B/one.md" ]'
check "both indexes survive the first merge" 'grep -q "win note" "$B/MEMORY.md" && grep -q "^- one" "$B/MEMORY.md"'
check "first merge is listed for tidying" 'tidy_has "$B" MEMORY.md'
sa pull >/dev/null
check "first machine receives the second's files" '[ -f "$A/win.md" ] && same'
sb tidied >/dev/null; sa tidied >/dev/null

echo "## everyday edits"
printf 'fact two\n' > "$A/two.md"; printf 'win two\n' > "$B/win2.md"
sa push; sb push; sa pull >/dev/null
check "edits to different files on both machines both land" '[ -f "$A/win2.md" ] && [ -f "$B/two.md" ] && same'
check "no tidy entry for a clean sync" '[ ! -s "$A/.git/memsync/tidy" ] && [ ! -s "$B/.git/memsync/tidy" ]'

echo "## same line changed on both machines"
sed -i.bak 's/fact one/fact one, mac version/' "$A/one.md"; rm -f "$A/one.md.bak"
sed -i.bak 's/fact one/fact one, win version/' "$B/one.md"; rm -f "$B/one.md.bak"
sa push; sb push; sa pull > "$T/pull.json"
check "both versions are kept" 'grep -q "mac version" "$B/one.md" && grep -q "win version" "$B/one.md"'
check "no conflict markers anywhere" no_markers
check "machines agree afterwards" same
check "the file is listed for tidying" 'tidy_has "$B" one.md'
sb pull > "$T/pull-b.json"
check "session start tells Claude what to tidy (valid JSON)" 'python3 -m json.tool "$T/pull-b.json" >/dev/null && grep -q "one.md" "$T/pull-b.json"'
sb tidied >/dev/null
check "tidied clears the list" '[ ! -s "$B/.git/memsync/tidy" ]'

echo "## deleted on one machine, changed on the other"
rm "$A/two.md"; sa push
printf 'fact two, edited on win\n' > "$B/two.md"; sb push; sa pull >/dev/null
check "the edited version is kept, not lost" 'grep -q "edited on win" "$A/two.md" && grep -q "edited on win" "$B/two.md"'
check "machines agree afterwards" same

echo "## non-text file changed on both machines"
printf '{"a":2}\n' > "$A/settings.json"; sa push
printf '{"a":3}\n' > "$B/settings.json"; sb push; sa pull >/dev/null
check "one version in place, the other kept as a copy" 'ls "$B"/settings.conflict-win-*.json >/dev/null 2>&1 && grep -q 2 "$B/settings.json"'
check "the copy is listed for tidying" 'tidy_has "$B" settings.json'
check "no conflict markers anywhere" no_markers
check "machines agree afterwards" same

echo "## many sessions syncing at once"
for i in 1 2 3 4 5 6; do printf 'n%s\n' "$i" > "$A/par$i.md"; sa push & done
for i in 1 2 3 4 5 6; do printf 'w%s\n' "$i" > "$B/wpar$i.md"; sb push & done
wait; sa push; sb push; sa push
check "all twelve parallel files reach both machines" '[ "$(ls "$A"/par*.md "$A"/wpar*.md | wc -l | tr -d " ")" = 12 ] && [ "$(ls "$B"/par*.md "$B"/wpar*.md | wc -l | tr -d " ")" = 12 ]'
check "machines agree afterwards" same
check "no failed attempts recorded" '[ "$(cat "$A/.git/memsync/fails")" = 0 ] && [ "$(cat "$B/.git/memsync/fails")" = 0 ]'
check "no lock left behind" '[ ! -d "$A/.git/memsync/lock" ] && [ ! -d "$B/.git/memsync/lock" ]'

echo "## a credential pasted into memory"
printf 'my token ghp_%s\n' "$(printf 'x%.0s' $(seq 1 36))" > "$A/oops.md"
sa push
check "the file is not pushed" '! git -C "$R" cat-file -e main:oops.md 2>/dev/null'
check "other files still sync" 'printf "ok\n" > "$A/fine.md"; sa push; git -C "$R" cat-file -e main:fine.md'
sa pull > "$T/held.json"
check "session start says it was held back" 'grep -q "oops.md" "$T/held.json"'
rm "$A/oops.md"

echo "## offline"
git -C "$A" remote set-url origin "$T/missing.git"
for i in 1 2 3; do sa push; done
sa pull > "$T/offline.json"; rc=$?
check "never fails the session" '[ $rc = 0 ]'
check "warns after three failed attempts" 'grep -q systemMessage "$T/offline.json" && python3 -m json.tool "$T/offline.json" >/dev/null'
git -C "$A" remote set-url origin "$R"; printf 'back\n' > "$A/back.md"; sa push
check "recovers once the remote is back" '[ "$(cat "$A/.git/memsync/fails")" = 0 ] && git -C "$R" cat-file -e main:back.md'

echo "## a sync killed half way"
sb pull >/dev/null
printf 'x\n' > "$A/k.md"; sa push; printf 'y\n' > "$B/k.md"; git -C "$B" add -A; git -C "$B" commit -qm local
git -C "$B" fetch -q; git -C "$B" -c merge.memsync.driver=false rebase -q origin/main >/dev/null 2>&1
check "(setup) rebase left open" '[ -d "$B/.git/rebase-merge" ] || [ -d "$B/.git/rebase-apply" ]'
sb push
check "next sync undoes it and completes" '[ ! -d "$B/.git/rebase-merge" ] && [ ! -d "$B/.git/rebase-apply" ] && grep -q x "$B/k.md" && grep -q y "$B/k.md"'
check "no conflict markers anywhere" no_markers
sa pull >/dev/null
check "machines agree afterwards" same

echo "## a clone without memsync (plain git, e.g. a cloud job)"
C="$T/c"; git clone -q "$R" "$C"
printf 'from plain git\n' > "$C/plain.md"; git -C "$C" add -A; git -C "$C" commit -qm plain; git -C "$C" push -q
sa pull >/dev/null
check "its pushes are picked up" '[ -f "$A/plain.md" ]'
printf 'mac line\n' >> "$A/MEMORY.md"; sa push
printf 'plain line\n' >> "$C/MEMORY.md"; git -C "$C" commit -qam plain2
git -C "$C" pull -q --rebase >/dev/null 2>&1; rc=$?
check "plain git still works on the same repo (it just reports its own conflict)" '[ $rc = 0 ] || [ -d "$C/.git/rebase-merge" ] || [ -d "$C/.git/rebase-apply" ]'

echo "## not set up"
out=$(MEMSYNC_DIR="$T/nowhere" bash "$S/memsync.sh" pull; echo "rc=$?")
check "a folder that is not set up is left alone, silently" '[ "$out" = "rc=0" ]'

echo
echo "$pass passed, $fail failed"
[ "$fail" = 0 ]
