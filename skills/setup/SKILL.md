---
name: setup
description: Set up Surya Memory, one memory for every project and AI tool, synced across machines. Use when the user asks to set up memory, sync memory across machines, share memory with Codex or other tools, or to set up memory sync.
---

# Set up Surya Memory

Setup brings the memory scattered across this machine's projects into one folder, connects the AI tools it finds, and turns on sync through a private GitHub repo the user owns.

1. Show the plan. This changes nothing:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/surya-memory" setup
   ```

   Tell the user in plain words what it found and what it will do. Mention any name clashes. Both versions are kept.

2. Ask only what the plan leaves open:
   - Do they want sync across machines? If not, add `--no-sync`.
   - If yes, run `gh auth status`. If `gh` is missing, tell them how to install it (`brew install gh` on a Mac). If it is not signed in, ask them to run `gh auth login` themselves. Never handle their GitHub password or token.
   - Is the memory already in a repo from another machine? If so, add `--repo NAME`.

3. Once they say yes, run the same command with `--yes` and the flags from step 2. The originals are left where they are, and any settings file it changes is backed up first.

4. Tell them in two or three sentences what happened: where the memory now lives, which tools are connected, and that they should restart those tools. On other machines they install this plugin and do the same with the same repo name.

If a step fails, show its message. Don't retry with different flags without asking.

To sync one project's memory folder and nothing else, without one shared store, run `bash "${CLAUDE_PLUGIN_ROOT}/scripts/setup.sh"` instead.
