---
name: setup
description: Turn on memory sync for this machine. Use when the user asks to sync, back up or share Claude's memory across machines, or to set up memory-sync.
---

# Set up memory sync

Memory sync keeps this project's Claude Code memory folder in a private GitHub repo that the user owns, and syncs it when a session starts and after each reply.

1. Check the GitHub CLI: run `gh auth status`. If `gh` is missing, tell the user how to install it (`brew install gh` on a Mac). If it is not signed in, ask them to run `gh auth login` themselves. Never handle their GitHub password or token.
2. Ask one question: is this the first machine, or is the memory already in a repo from another machine? If it is already in a repo, get the repo name.
3. Run the setup script, adding `--repo NAME` when they named one:

   ```bash
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/setup.sh"
   ```

   It creates a private repo called `agent-memory` if none exists, refuses a public one, merges any memory already on this machine instead of replacing it, and pushes.
4. Tell the user in two or three plain sentences what happened: the folder, the repo, and that it now syncs by itself. On their other machines they install this plugin and say "set up memory sync" with the same repo name.

If the script fails, show its message and the last lines of `.git/memsync/log` in the memory folder. Don't retry with different flags without asking.
