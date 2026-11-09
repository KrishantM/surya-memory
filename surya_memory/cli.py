"""The `surya-memory` command."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from . import __version__, connect as connections, onestore, server
from .store import MemoryError_, Store, resolve_store


def _store(args: argparse.Namespace) -> Store:
    return Store(resolve_store(getattr(args, "dir", None)))


def _sync_script() -> Path:
    return Path(__file__).resolve().parent.parent / "scripts"


def perform_setup(target: Path, repo: str = "agent-memory", remote: Optional[str] = None, sync: bool = True, connect: bool = True) -> list:
    """Do the whole setup and report each step. A failed step never undoes the others."""
    steps = []
    plan = onestore.make_plan(target)
    onestore.apply_plan(plan)
    onestore.pin_store(target)
    runtime = connections.install_runtime()
    steps.append({"step": "One store", "ok": True, "detail": f"{len(plan.copies)} file(s) brought into {target}"})
    if sync:  # from the stable copy, because the store's git config records where its merge driver lives
        command = ["bash", str(runtime / "scripts" / "setup.sh"), "--dir", str(target), "--repo", repo]
        if remote:
            command += ["--remote", remote]
        done = subprocess.run(command, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        detail = (done.stderr.strip() or done.stdout.strip()).splitlines()
        steps.append({"step": "Sync", "ok": done.returncode == 0, "detail": "On" if done.returncode == 0 else (detail[-1] if detail else "Failed")})
    else:
        Store(target)._ensure_repo()  # undo and the change list work without sync
    if connect:
        for result in connections.connect():
            steps.append({"step": result["label"], "ok": result["ok"], "detail": "Connected" if result["ok"] else result["error"]})
    return steps


def cmd_setup(args: argparse.Namespace) -> int:
    target = Path(args.store).expanduser() if args.store else onestore.default_store()
    plan = onestore.make_plan(target)
    print(f"One memory for every project: {target}")
    if plan.sources:
        print("Memory found on this machine:")
        for source in plan.sources:
            print(f"  {source.label}: {len(source.files)} file(s) in {source.path}")
    else:
        print("No existing memory found on this machine.")
    print(f"Will copy {len(plan.copies)} file(s). {plan.same} already there. Originals are left where they are.")
    for label, old, new in plan.renames:
        print(f"  Same name, different content: {old} from {label} becomes {new}")
    found = [t["label"] for t in connections.status() if t["detected"]]
    if not args.no_connect:
        print("Will connect: " + (", ".join(found) if found else "no AI tools found"))
    print("Will also set autoMemoryDirectory in ~/.claude/settings.json (backed up first).")
    if not args.no_sync:
        print(f"Will sync through a private GitHub repo called {args.repo}." if not args.remote else f"Will sync through {args.remote}.")
    if not args.yes:
        print("\nNothing has changed yet. Run again with --yes to do this.")
        return 0
    steps = perform_setup(target, args.repo, args.remote, not args.no_sync, not args.no_connect)
    for step in steps:
        print(f"  {step['step']}: {step['detail']}" if step["ok"] else f"  {step['step']}: FAILED ({step['detail']})")
    print("\nDone. Every project now shares one memory. Restart your AI tools so they pick it up.")
    return 0 if all(s["ok"] for s in steps) else 1


def cmd_connect(args: argparse.Namespace) -> int:
    if args.list or (not args.tools and not args.all):
        for tool in connections.status():
            state = "connected" if tool["connected"] else ("found, not connected" if tool["detected"] else "not found")
            print(f"{tool['id']:15} {state}" + (f"  ({tool['note']})" if tool["note"] and tool["detected"] else ""))
        return 0
    results = connections.disconnect(args.tools) if args.disconnect else connections.connect(args.tools or None)
    for result in results:
        print(f"{result['label']}: " + ("done" if result["ok"] else f"failed ({result['error']})"))
    return 0 if all(r["ok"] for r in results) else 1


def cmd_search(args: argparse.Namespace) -> int:
    for item in _store(args).search(args.query, limit=args.limit):
        print(f"{item['name']}: {item['description']}")
    return 0


def cmd_recall(args: argparse.Namespace) -> int:
    print(_store(args).recall(args.name, depth=args.depth))
    return 0


def cmd_changes(args: argparse.Namespace) -> int:
    for change in _store(args).changes(args.limit):
        when = time.strftime("%d %b %H:%M", time.localtime(change["when"]))
        print(f"{change['short']}  {when}  {change['subject']}")
    return 0


def cmd_undo(args: argparse.Namespace) -> int:
    print(f"Undid {_store(args).undo(args.id, args.file)['undid']}")
    return 0


def cmd_tidy(args: argparse.Namespace) -> int:
    print(json.dumps(_store(args).tidy(), indent=1, ensure_ascii=False))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    store = _store(args)
    stats = store.stats()
    print(f"Store:    {store.root}")
    print(f"Memories: {stats['memories']} ({stats['archived']} archived, {stats['stale']} may be out of date)")
    print(f"Index:    {stats['index']['lines']} of {stats['index']['max_lines']} lines")
    print("Syncing:  " + ("yes" if store.syncing() else "no"))
    if store.syncing():
        subprocess.run(["bash", str(_sync_script() / "memsync.sh"), "status", "--dir", str(store.root)])
    return 0


def cmd_app(args: argparse.Namespace) -> int:
    from . import app
    if args.exit_with_parent:  # the Mac app holds our stdin open, so end of input means it's gone
        threading.Thread(target=lambda: (sys.stdin.read(), os._exit(0)), daemon=True).start()
    return app.run(Path(args.dir).expanduser() if args.dir else None, port=args.port, open_browser=not args.no_open)


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="surya-memory", description="One memory, in plain files you own, for every AI tool you use.")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, handler, help_text: str) -> argparse.ArgumentParser:
        command = sub.add_parser(name, help=help_text)
        command.set_defaults(handler=handler)
        command.add_argument("--dir", help="Use this memory folder")
        return command

    setup = add("setup", cmd_setup, "Move to one store for every project, turn on sync, connect your tools")
    setup.add_argument("--store", help="Where the store goes (default ~/agent-memory)")
    setup.add_argument("--repo", default="agent-memory", help="Private GitHub repo to sync through")
    setup.add_argument("--remote", help="Use this git remote instead of GitHub")
    setup.add_argument("--no-sync", action="store_true")
    setup.add_argument("--no-connect", action="store_true")
    setup.add_argument("--yes", action="store_true", help="Do it. Without this, setup only shows the plan")
    command = add("connect", cmd_connect, "Plug the memory into your AI tools")
    command.add_argument("tools", nargs="*")
    command.add_argument("--all", action="store_true", help="Every tool found on this machine")
    command.add_argument("--list", action="store_true")
    command.add_argument("--disconnect", action="store_true")
    add("serve", lambda a: server.serve(a.dir), "Run the MCP server (your AI tools start this)")
    add("refresh-runtime", lambda a: connections.refresh_runtime() or 0, "Update the copy other tools run")
    add("status", cmd_status, "Where the memory is and whether it is syncing")
    command = add("search", cmd_search, "Find memories")
    command.add_argument("query")
    command.add_argument("--limit", type=int, default=10)
    command = add("recall", cmd_recall, "Read the index, or a memory and its neighbours")
    command.add_argument("name", nargs="?")
    command.add_argument("--depth", type=int, default=1)
    command = add("changes", cmd_changes, "What changed recently")
    command.add_argument("--limit", type=int, default=20)
    command = add("undo", cmd_undo, "Reverse a change")
    command.add_argument("id", nargs="?")
    command.add_argument("--file")
    add("tidy", cmd_tidy, "Report what needs attention")
    command = add("app", cmd_app, "Open the memory app")
    command.add_argument("--port", type=int, default=0)
    command.add_argument("--no-open", action="store_true")
    command.add_argument("--exit-with-parent", action="store_true", help=argparse.SUPPRESS)

    args = parser.parse_args(argv)
    try:
        return int(args.handler(args) or 0)
    except MemoryError_ as error:
        print(error, file=sys.stderr)
        return 1
