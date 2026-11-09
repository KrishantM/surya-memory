# Surya Memory

One memory, in plain files you own, for Claude Code, Codex and your other AI tools. It syncs across your machines through your own private GitHub repo. You can see it, talk to it and undo anything an AI wrote.

- **One store.** Every project and every tool reads and writes the same folder of Markdown files.
- **Your repo, no server.** Sync goes to a private repo in your GitHub account. There is no account to create and nothing runs on anyone else's server.
- **Nothing is lost.** Each write is one git commit, labelled with the tool that made it. Forgetting moves a note to an archive. When two machines change the same note, both versions are kept.
- **You can check it.** The app shows every memory, what each tool changed, and an undo beside each change.
- **Small.** Python standard library, git, and nothing else.

## Install

You need git and Python 3.9 or newer. Sync also needs the [GitHub CLI](https://cli.github.com), signed in.

**As a Claude Code plugin.** In Claude Code:

```
/plugin marketplace add KrishantM/surya-memory
/plugin install surya-memory@surya
```

Then say **"set up my memory"**. Claude shows you the plan first. Nothing changes until you agree.

**As a Mac app.** Build it from source. This needs the Xcode command line tools:

```bash
git clone https://github.com/KrishantM/surya-memory
```

```bash
bash surya-memory/app/macos/build.sh
```

The app lands in `dist/`. Open it and it walks you through setup. It isn't notarised yet, so there is no download.

**From a terminal.**

```bash
python3 scripts/surya-memory setup
```

That prints the plan. Add `--yes` to carry it out, or `--no-sync` to stay on this machine. `python3 scripts/surya-memory app` opens the app in your browser.

On each other machine, install it the same way and use the same repo name (`--repo NAME`). Memory already on that machine is merged, not replaced.

**Upgrading from 0.1.** The plugin was called `memory-sync`. Uninstall it and install `surya-memory`. Sync keeps working per project until you run setup.

## What setup does

1. Copies the memory from each Claude Code project into one folder, `~/agent-memory`. The originals stay where they are. If two projects have a note with the same name and different text, both are kept under different names.
2. Points Claude Code at that folder with `autoMemoryDirectory`. Your settings file is backed up first.
3. Connects the other AI tools it finds.
4. Turns on sync, if you want it.

## Tools

| Tool | How it connects | Status |
|---|---|---|
| Claude Code | Native memory, plus the plugin or MCP server | Run for real |
| Codex CLI | MCP server, and a pointer in `AGENTS.md` | Run for real |
| Claude Desktop | MCP server | Config is written and unit tested, not yet run for real. Quit it before connecting, because a running copy rewrote the file in our one real try |
| Cursor, Windsurf, Gemini CLI | MCP server | Config is written and unit tested, not yet run for real |
| ChatGPT and claude.ai on the web | Not supported | Web chats can't read local files. They would need a hosted endpoint |

`python3 scripts/surya-memory connect --list` shows what it found. `python3 scripts/surya-memory connect codex` connects one tool, and `--disconnect` removes it.

## What AI tools can do

| Tool | What it does |
|---|---|
| `recall` | Read the index, or a memory and the ones it links to |
| `search` | Find memories by words |
| `remember` | Save a new memory |
| `amend` | Correct a memory |
| `archive`, `restore` | Forget a memory, or bring it back |
| `changes`, `undo` | See what changed, and reverse it |
| `tidy` | Report duplicates, stale notes and index problems |

## The app

- **Memories:** browse, search, read, edit, add and forget.
- **Talk:** ask what your memory says, or tell it what to change. It uses the Claude or Codex program you are already signed in to, and it can only use the memory tools.
- **Changes:** what changed, which tool changed it, the exact diff, and undo for a whole change or one file.
- **Links:** how your memories connect.
- **Health:** the same report as `tidy`, with a button to have AI fix it.
- **Tools:** connect and disconnect your AI tools.

## How memory is stored

Everything is Markdown with a small header, the same format Claude Code writes, so nothing needs migrating. You can also open the folder as an Obsidian vault.

```markdown
---
name: deploy-steps
description: How the app deploys, and the one step that isn't automated
metadata:
  type: project
  scope: global
---

Merge to main. CI runs the tests. Run the deploy by hand. See [[harbour-app]].
```

- **One fact or topic per file.** Link related notes with `[[their-name]]`.
- **`type`:** `user`, `feedback`, `project` or `reference`. Project notes are flagged as possibly out of date after 30 days, references after 90.
- **`scope`:** `global`, `project:name` or `machine:name`. Notes from another machine are hidden, and notes from another project rank lower.
- **`MEMORY.md`** is the index, one line per memory. Claude Code loads the first 200 lines or 25KB, so the app shows how full it is.
- **`archive/`** holds forgotten notes.

Markdown is the only source of truth. A log that grows row by row can sit beside its summary as CSV or JSONL. Search needs no index, so there is no database to rebuild or sync.

## What happens when two machines disagree

| Situation | What it does |
|---|---|
| Different files changed | Both changes land |
| Same lines of a note changed on both | Both versions kept in the note, file listed for tidying |
| Note deleted on one machine, edited on the other | The edited version is kept |
| A non-text file changed on both | One version in place, the other saved as `name.conflict-<machine>-<time>.ext` |
| A file that looks like it holds a credential | Not synced. You're told which file |
| Several sessions syncing at once | They take turns |
| A sync killed half way | The next sync undoes it and finishes |

It never writes `<<<<<<<` conflict markers into a note, because AI tools read memory as fact.

## Safety

- **The app** listens on 127.0.0.1 only. Each launch has its own token, and it checks Host and Origin, so another site open in your browser can't reach it. The page's content security policy blocks anything a note might try to load.
- **Talk** never holds a token of its own. It runs the official CLIs you are signed in to, with every tool turned off except memory.
- **Credentials.** Writes that look like a token or key are refused, and the sync holds back any file that looks like one. This catches common formats, not every secret, so keep secrets out of memory.
- **Text inside a memory is data.** AI tools are told not to follow instructions found in memories. That lowers the risk of a planted note, but no model obeys it every time. The Changes view is there so you can check.
- **Keep the sync repo private.** Setup refuses a public one.

## Without the plugin

The sync scripts work on their own. Run `scripts/setup.sh --dir <memory folder>` once per machine, then add these hooks to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [{ "hooks": [{ "type": "command", "command": "MEMSYNC_DIR=<memory folder> bash <path>/scripts/memsync.sh pull" }] }],
    "Stop": [{ "hooks": [{ "type": "command", "command": "MEMSYNC_DIR=<memory folder> bash <path>/scripts/memsync.sh push" }] }]
  }
}
```

## Tests

```bash
bash tests/run.sh
```

```bash
python3 -m unittest discover -s tests
```

The first simulates two machines and a shared remote: concurrent sessions, conflicts, going offline, credentials and interrupted syncs. The second covers the store, the MCP server, setup, tool connections and the app's API.

## Limits

- Built and tested on macOS. Linux should work. Windows is untested.
- The Mac app is built from source for now. It is not signed or notarised.
- Search is by words. It doesn't do embeddings.
- Tools without hooks, such as Codex, pull from sync when a memory tool is called, at most every five minutes.

MIT licensed.
