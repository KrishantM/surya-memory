"""Bring every project's separate memory folder into one store.

Claude Code keeps a memory folder per project, so what it learns in one project
is invisible in the next. Setting `autoMemoryDirectory` points every project at
one folder, and this moves what already exists into it.

Nothing is overwritten and the original folders are left untouched. Running it
twice changes nothing the second time.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .store import INDEX, _git_root, parse_frontmatter, set_frontmatter, surya_home

DEFAULT_STORE = "agent-memory"
SKIP_NAMES = {INDEX, ".memsync", ".gitattributes", ".gitignore", ".DS_Store"}
SKIP_DIRS = {".git", ".obsidian", ".trash"}


@dataclass
class Source:
    path: Path
    label: str
    files: list = field(default_factory=list)


@dataclass
class Plan:
    store: Path
    sources: list
    copies: list = field(default_factory=list)   # (source file, relative path, scope or None, source label)
    renames: list = field(default_factory=list)  # (label, old relative, new relative)
    same: int = 0


def default_store() -> Path:
    return Path.home() / DEFAULT_STORE


def _project_label(project_dir: Path) -> str:
    """The folder name flattens every symbol to '-', which can't be reversed.
    Sessions record their real working directory, so read it from there."""
    sessions = sorted(project_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    for session in sessions[:5]:
        try:
            with session.open(encoding="utf-8", errors="replace") as handle:
                for _, line in zip(range(40), handle):
                    found = re.search(r'"cwd"\s*:\s*"([^"]+)"', line)
                    if found:
                        cwd = Path(found.group(1))
                        return ((_git_root(cwd) if cwd.is_dir() else None) or cwd).name
        except OSError:
            continue
    return project_dir.name.rsplit("-", 1)[-1] or project_dir.name


def _files(folder: Path) -> list:
    found = []
    for base, dirs, names in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(names):
            if name not in SKIP_NAMES and ".conflict-" not in name and not name.startswith(".tmp-"):
                found.append(Path(base) / name)
    return found


def discover(store: Path) -> list:
    root = Path.home() / ".claude" / "projects"
    sources = []
    if root.is_dir():
        for project_dir in sorted(root.iterdir()):
            memory = project_dir / "memory"
            if memory.is_dir() and memory.resolve() != store.resolve():
                files = _files(memory)
                if files or (memory / INDEX).is_file():
                    sources.append(Source(memory, _project_label(project_dir), files))
    # Largest first, so the memory with most to lose keeps its original names.
    sources.sort(key=lambda s: -len(s.files))
    labels: dict = {}
    for source in sources:
        labels[source.label] = labels.get(source.label, 0) + 1
        if labels[source.label] > 1:
            source.label = f"{source.label}-{labels[source.label]}"
    return sources


def _same(a: Path, b: Path) -> bool:
    if a.suffix != ".md":
        return a.read_bytes() == b.read_bytes()
    read = lambda p: parse_frontmatter(p.read_text(encoding="utf-8", errors="replace"))[1]
    return read(a) == read(b)


def _scope_for(file: Path, label: str, many: bool) -> Optional[str]:
    """Facts about the person hold everywhere. Project notes stay with their project."""
    if not many or file.suffix != ".md":
        return None
    meta, _ = parse_frontmatter(file.read_text(encoding="utf-8", errors="replace"))
    return None if meta.get("type") in ("user", "feedback") or meta.get("scope") else f"project:{label}"


def make_plan(store: Path, sources: Optional[list] = None) -> Plan:
    sources = discover(store) if sources is None else sources
    plan = Plan(store=store, sources=sources)
    many = len(sources) > 1
    taken = {p.relative_to(store).as_posix(): p for p in _files(store)} if store.is_dir() else {}
    incoming: dict = {}
    for source in sources:
        for file in source.files:
            relative = file.relative_to(source.path).as_posix()
            scope = _scope_for(file, source.label, many)
            existing = taken.get(relative) or incoming.get(relative)
            if existing is None:
                plan.copies.append((file, relative, scope, source.label))
                incoming[relative] = file
                continue
            if _same(file, existing):
                plan.same += 1
                continue
            stem, suffix = os.path.splitext(relative)
            renamed, counter = f"{stem}--{source.label}{suffix}", 2
            while True:
                clash = taken.get(renamed) or incoming.get(renamed)
                if clash is None or _same(file, clash):
                    break
                renamed, counter = f"{stem}--{source.label}-{counter}{suffix}", counter + 1
            plan.renames.append((source.label, relative, renamed))
            if clash is None:
                plan.copies.append((file, renamed, scope, source.label))
                incoming[renamed] = file
            else:
                plan.same += 1
    return plan


def _merged_index(plan: Plan) -> Optional[str]:
    target = plan.store / INDEX
    text = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else ""
    original = text
    for source in plan.sources:
        path = source.path / INDEX
        if not path.is_file():
            continue
        incoming = path.read_text(encoding="utf-8", errors="replace").strip()
        if not incoming:
            continue
        for label, old, new in plan.renames:
            if label == source.label:
                incoming = incoming.replace(f"({old})", f"({new})")
        if incoming in text:
            continue
        heading = f"## From {source.label}"
        if len(plan.sources) == 1 and not text.strip():
            text = incoming + "\n"
        elif heading in text:
            continue
        else:
            text = (text.rstrip() + "\n\n" if text.strip() else "") + f"{heading}\n\n{incoming}\n"
    return text if text != original else None


def apply_plan(plan: Plan) -> None:
    plan.store.mkdir(parents=True, exist_ok=True)
    for file, relative, scope, _label in plan.copies:
        destination = plan.store / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            continue  # never overwrite, even if the folder changed after planning
        shutil.copy2(file, destination)
        if destination.suffix == ".md":
            text = destination.read_text(encoding="utf-8", errors="replace")
            updates: dict = {}
            if scope:
                updates["scope"] = scope
            if "--" in destination.stem and relative != file.name and parse_frontmatter(text)[0].get("name"):
                updates["name"] = destination.stem
            if updates:
                destination.write_text(set_frontmatter(text, updates), encoding="utf-8")
    merged = _merged_index(plan)
    if merged is not None:
        (plan.store / INDEX).write_text(merged, encoding="utf-8")


def backup(path: Path) -> Optional[Path]:
    if not path.is_file():
        return None
    folder = surya_home() / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    copy = folder / f"{path.parent.name}-{path.name}.{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(path, copy)
    return copy


def pin_store(store: Path) -> None:
    """Tell every surface where the one store is."""
    home = surya_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "store-path").write_text(str(store) + "\n", encoding="utf-8")
    settings = Path.home() / ".claude" / "settings.json"
    data: dict = {}
    if settings.is_file():
        try:
            data = json.loads(settings.read_text(encoding="utf-8"))
        except ValueError:
            raise SystemExit(f"{settings} is not valid JSON, so it was left alone. Fix it and run setup again.")
    if data.get("autoMemoryDirectory") == str(store):
        return
    backup(settings)
    data["autoMemoryDirectory"] = str(store)
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
