#!/usr/bin/env bash
# Git merge driver for memory files, registered as "memsync" for *.md.
# Git calls it with: ancestor, ours (the result is written here), theirs, path.
#
# It tries a normal three-way merge first. Where both machines changed the same
# lines, it keeps both versions instead of writing conflict markers, and lists
# the file so the next session can tidy it.
O=$1 A=$2 B=$3 P=$4
tmp=$(mktemp) || exit 1
cp "$A" "$tmp" || { rm -f "$tmp"; exit 1; }
if git merge-file -q "$A" "$O" "$B"; then rm -f "$tmp"; exit 0; fi
cp "$tmp" "$A"; rm -f "$tmp"
git merge-file -q --union "$A" "$O" "$B" || exit 1
d=$(git rev-parse --absolute-git-dir 2>/dev/null) && mkdir -p "$d/memsync" &&
  printf 'both\t%s\tchanged on two machines: both versions kept, check for duplicated or contradictory lines\n' "$P" >> "$d/memsync/tidy"
exit 0
