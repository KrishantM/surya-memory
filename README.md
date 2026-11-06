# Surya Memory

Claude Code remembers things about you and your projects in a memory folder on each machine. This plugin keeps that folder the same on all your machines, through a private GitHub repo that you own.

- **Your repo, no server.** Memory goes to a private repo in your GitHub account. Nothing runs on anyone else's server, and the setup refuses a public repo.
- **Nothing is lost.** When two machines change the same note, both versions are kept. Claude is told which files to tidy at the start of the next session.
- **Never breaks a session.** Sync runs when a session starts and after each reply. If you're offline it retries later, and after three failures it tells you.
- **Plain files.** Memory stays as Markdown, so you can read and edit it in any editor or open it as an Obsidian vault.

## Install

You need [Claude Code](https://code.claude.com), git, and the [GitHub CLI](https://cli.github.com) signed in (`gh auth login`).

In Claude Code:

```
/plugin marketplace add KrishantM/surya-memory
/plugin install memory-sync@surya
```

Then say **"set up memory sync"**. Claude runs the setup, which creates a private repo called `agent-memory` and pushes your memory to it.

On each other machine, install the plugin the same way and say "set up memory sync" again. Memory already on that machine is merged with the repo, not replaced.

## What happens when two machines disagree

| Situation | What it does |
|---|---|
| Different files changed | Both changes land. Nothing to do. |
| Same lines of a note changed on both | Both versions kept in the note, file listed for tidying |
| Note deleted on one machine, edited on the other | The edited version is kept |
| A non-text file changed on both | One version in place, the other saved as `name.conflict-<machine>-<time>.ext` |
| A file that looks like it holds a credential | Not synced. You're told which file. |
| Several sessions syncing at once | They take turns, one at a time |
| A sync killed half way | The next sync undoes it and finishes |

It never writes `<<<<<<<` conflict markers into a note, because Claude reads memory as fact.

Ask "is my memory in sync?" at any time, or run `scripts/memsync.sh status`.

## Without the plugin

The scripts work on their own. Run `scripts/setup.sh --dir <memory folder>` once per machine. Then add these hooks to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [{ "hooks": [{ "type": "command", "command": "MEMSYNC_DIR=<memory folder> bash <path>/scripts/memsync.sh pull" }] }],
    "Stop": [{ "hooks": [{ "type": "command", "command": "MEMSYNC_DIR=<memory folder> bash <path>/scripts/memsync.sh push" }] }]
  }
}
```

Leave out `MEMSYNC_DIR` to sync the memory folder of whichever project the session is in.

## How it works

- **The sync.** Each sync commits local edits, fetches, and replays them on top of the remote. If that can't be done cleanly, it does a single merge instead.
- **Notes changed on both machines.** A git merge driver handles overlapping edits in `.md` files by keeping both sides.
- **Files the driver can't merge.** Anything left over is kept as a copy, never dropped.
- **Per-machine state.** The log, the tidy list and the lock live in `.git/memsync`, so they are never synced.
- **Plain git users.** A clone without the plugin, such as a scheduled cloud job, can still push to the same repo with plain git.

Tests: `bash tests/run.sh` runs 36 checks with two simulated machines and a shared remote. They cover concurrent sessions, conflicts, going offline, credentials and interrupted syncs.

## Limits

- macOS and Linux. Windows should work under Git Bash but hasn't been tested.
- Claude Code only for now.
- Memory holds personal details. Keep the repo private, and don't put secrets in memory. The credential check catches common token formats, not every secret.

MIT licensed.
