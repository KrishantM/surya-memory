---
name: status
description: Check whether memory is in sync across machines, and tidy files that were merged from two machines. Use when the user asks if memory is synced, or when a session-start note says memory sync merged edits or failed.
---

# Memory sync status

Run:

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/memsync.sh" status
```

Explain the result in one or two sentences: in sync or not, and when it last synced.

**If files are listed "to tidy"**, two machines changed them at the same time and both versions were kept. For each one:
- Read it. Remove lines that are duplicated, and where two lines disagree, keep the newer or more specific fact. If you can't tell which is right, ask the user.
- For a `*.conflict-*` copy, fold anything still useful into the original file, then delete the copy.

Then clear the list:

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/memsync.sh" tidied
```

**If files are "held back"**, they look like they contain a credential and were not synced. Tell the user which file and line. Don't print the secret back. Removing it lets the file sync.

**If syncing keeps failing**, show the recent log lines from the status output. Common causes: offline, or GitHub access expired (`gh auth status`).
