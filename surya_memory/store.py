"""The memory store: plain Markdown files in a folder, usually a git repo.

The folder is the only source of truth. Everything here reads it fresh, so there
is no index or database that can drift from what the files say.

The format is the one Claude Code's auto memory already writes: a MEMORY.md
index with one line per memory, and one file per fact with a small block of
frontmatter. Extra fields go under `metadata:`, where Claude Code ignores them.

Every write is one git commit with a readable message, which is what makes
"what changed?" and "undo that" possible from any tool.
"""

from __future__ import annotations

import json
import math
import os
import re
import socket
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

INDEX = "MEMORY.md"
STARTING_POINT = "starting point"
ARCHIVE = "archive"
SKIP_DIRS = {".git", ".obsidian", ".trash", "node_modules"}
HIDDEN_IN_CHANGES = {".memsync", ".gitattributes", ".gitignore"}
TYPES = ("user", "feedback", "project", "reference")

# Claude Code loads the first 200 lines or 25KB of the index, whichever is first.
INDEX_MAX_LINES = 200
INDEX_MAX_BYTES = 25_000

# How long a memory of each type stays trustworthy without checking. Who the
# user is and how they like to work rarely expire. Project state goes off fast.
STALE_DAYS = {"project": 30, "reference": 90}

# Keep in step with SECRET_RE in scripts/lib.sh.
SECRET_RE = re.compile(
    r"sk-ant-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{32,}|gh[pousr]_[A-Za-z0-9]{30,}"
    r"|github_pat_[A-Za-z0-9_]{30,}|AKIA[0-9A-Z]{16}|xox[abpr]-[A-Za-z0-9-]{10,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
)

_FRONTMATTER = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)", re.S)
_KEY = re.compile(r"^(\s*)([A-Za-z_][\w-]*)\s*:\s*(.*)$")
_WIKILINK = re.compile(r"\[\[([^\]|#]+?)(?:[|#][^\]]*)?\]\]")
_MDLINK = re.compile(r"\]\(([^)\s:]+?\.md)\)")
_NAME_OK = re.compile(r"^[a-z0-9][a-z0-9._-]{0,80}$")
_HASH_OK = re.compile(r"^[0-9a-f]{7,40}$")
_STOP = set("a an and are as at be but by for from has have i in is it its me my of on or that the this to was what when "
            "where which who why will with you your do does did about know".split())


class MemoryError_(Exception):
    """Something the caller can fix. The message is written to be shown as is."""


def surya_home() -> Path:
    return Path(os.environ.get("SURYA_MEMORY_HOME") or Path.home() / ".surya-memory")


def host_name() -> str:
    """The same name scripts/lib.sh uses, so a machine has one identity."""
    if os.environ.get("MEMSYNC_HOST"):
        return os.environ["MEMSYNC_HOST"]
    try:
        out = subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True, text=True, timeout=2)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return socket.gethostname().split(".")[0]


def _git_root(path: Path) -> Optional[Path]:
    try:
        out = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return Path(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else None


def current_project() -> str:
    start = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    return (_git_root(start) or start).name


def _project_memory_dir(project: Path) -> Path:
    return Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(project)) / "memory"


def _claude_setting_dir() -> Optional[Path]:
    try:
        data = json.loads((Path.home() / ".claude" / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = data.get("autoMemoryDirectory") if isinstance(data, dict) else None
    return Path(os.path.expanduser(value)) if isinstance(value, str) and value else None


def resolve_store(explicit: Optional[str] = None) -> Path:
    """Where the memory lives, the same way for every surface."""
    for value in (explicit, os.environ.get("SURYA_MEMORY_DIR"), os.environ.get("MEMSYNC_DIR")):
        if value and "${" not in value:  # an unset plugin option can arrive as the raw placeholder
            return Path(os.path.expanduser(value))
    pinned = surya_home() / "store-path"
    if pinned.is_file():
        value = pinned.read_text(encoding="utf-8").strip()
        if value:
            return Path(os.path.expanduser(value))
    configured = _claude_setting_dir()
    if configured:
        return configured
    start = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    return _project_memory_dir(_git_root(start) or start)


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Flat key/value pairs from the frontmatter, and the body after it.

    The shape is small and fixed, so parsing by hand avoids a YAML dependency.
    Nested keys are flattened: `metadata:\\n  type: user` reads as `type`.
    """
    match = _FRONTMATTER.match(text)
    if not match:
        return {}, text
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        key = _KEY.match(line)
        if key and key.group(3).strip():
            value = key.group(3).strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1].replace('\\"', '"') if value[0] == '"' else value[1:-1]
            meta[key.group(2).lower()] = value
    return meta, text[match.end():]


def _yaml_value(value: str) -> str:
    return json.dumps(value, ensure_ascii=False) if re.search(r"[:#\"'\[\]{}]|^\s|\s$", value) else value


def set_frontmatter(text: str, updates: dict[str, Optional[str]]) -> str:
    """Change fields in place and leave every other line exactly as it was.

    `name` and `description` are top-level. Everything else goes under
    `metadata:`, which is where Claude Code's own format keeps it. A value of
    None removes the field.
    """
    match = _FRONTMATTER.match(text)
    lines = match.group(1).splitlines() if match else []
    body = text[match.end():] if match else text
    for key, value in updates.items():
        top = key in ("name", "description")
        found = None
        for i, line in enumerate(lines):
            parsed = _KEY.match(line)
            if parsed and parsed.group(2).lower() == key and bool(parsed.group(1)) != top:
                found = i
                break
        if value is None:
            if found is not None:
                del lines[found]
            continue
        rendered = f"{key}: {_yaml_value(value)}" if top else f"  {key}: {_yaml_value(value)}"
        if found is not None:
            lines[found] = rendered
        elif top:
            lines.insert(0 if key == "name" else min(1, len(lines)), rendered)
        else:
            parent = next((i for i, l in enumerate(lines) if l.rstrip() == "metadata:"), None)
            if parent is None:
                lines.append("metadata:")
                parent = len(lines) - 1
            end = parent + 1
            while end < len(lines) and lines[end].startswith((" ", "\t")):
                end += 1
            lines.insert(end, rendered)
    return "---\n" + "\n".join(lines) + "\n---\n" + body


@dataclass
class Note:
    name: str
    path: str
    description: str
    type: str
    scope: str
    status: str
    source: str
    modified: float
    body: str
    text: str
    links: list = field(default_factory=list)
    archived: bool = False

    @property
    def title(self) -> str:
        return self.name.replace("-", " ").replace("_", " ")

    def age_days(self) -> int:
        return max(0, int((time.time() - self.modified) / 86400)) if self.modified else 0

    def stale(self) -> bool:
        limit = STALE_DAYS.get(self.type)
        return bool(limit and self.modified and self.age_days() > limit)

    def summary(self) -> dict[str, Any]:
        return {"name": self.name, "path": self.path, "title": self.title, "description": self.description,
                "type": self.type, "scope": self.scope, "status": self.status, "source": self.source,
                "modified": self.modified, "age_days": self.age_days(), "stale": self.stale(),
                "archived": self.archived, "chars": len(self.body), "links": self.links}


class Store:
    def __init__(self, root: Path, project: Optional[str] = None, host: Optional[str] = None):
        self.root = Path(root).expanduser()
        self.project = project if project is not None else current_project()
        self.host = host if host is not None else host_name()
        self._times: dict[str, float] = {}
        self._times_head = ""

    # ----- git ---------------------------------------------------------------

    def is_repo(self) -> bool:
        return (self.root / ".git").exists()

    def syncing(self) -> bool:
        return self.is_repo() and (self.root / ".memsync").is_file()

    def _git(self, *args: str, check: bool = True, stdin: Optional[str] = None) -> str:
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_MERGE_AUTOEDIT="no")
        out = subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, text=True, input=stdin,
                             timeout=30, env=env, encoding="utf-8", errors="replace")
        if check and out.returncode != 0:
            raise MemoryError_(f"git {args[0]} failed: {out.stderr.strip()[:300]}")
        return out.stdout

    def _ensure_repo(self) -> None:
        """Local history works even before sync is turned on."""
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.is_repo():
            self._git("init", "-q", "-b", "main")
        if not self._git("config", "user.email", check=False).strip():
            self._git("config", "user.name", self.host)
            self._git("config", "user.email", f"memsync@{self.host}.local")
        if not self._git("rev-parse", "-q", "--verify", "HEAD", check=False).strip():
            self._starting_point()

    def _starting_point(self, paths: Optional[list[str]] = None) -> None:
        """Give files with no history a first version, so undoing the first change to one brings it back.

        A file that looks like it holds a credential is left out of the store-wide
        pass, as the sync leaves it out. Changing one by name is refused.
        """
        listed = self._git("ls-files", "-o", "--exclude-standard", *(["--", *paths] if paths else [])).splitlines()
        keep = []
        for path in listed:
            try:
                self._check_secret((self.root / path).read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                continue
            except MemoryError_:
                if paths:
                    raise
                continue
            keep.append(path)
        if keep:
            self._commit(STARTING_POINT, keep)

    @contextmanager
    def lock(self) -> Iterator[None]:
        """The same lock the sync takes, so a write can never land mid-rebase."""
        if not self.is_repo():
            yield
            return
        state = self.root / ".git" / "memsync"
        state.mkdir(parents=True, exist_ok=True)
        lock = state / "lock"
        deadline = time.time() + 30
        while True:
            try:
                lock.mkdir()
                break
            except FileExistsError:
                try:
                    if time.time() - lock.stat().st_mtime > 120:
                        lock.rmdir()
                        continue
                except OSError:
                    continue
                if time.time() > deadline:
                    raise MemoryError_("The memory folder is busy syncing. Try again in a moment.")
                time.sleep(0.2)
        try:
            yield
        finally:
            try:
                lock.rmdir()
            except OSError:
                pass

    def _commit(self, message: str, paths: list[str]) -> str:
        for path in paths:  # one at a time: a path git has never seen is not an error here
            self._git("add", "-A", "--", path, check=False)
        if not self._git("diff", "--cached", "--name-only").strip():
            return ""
        self._git("commit", "-q", "-m", message)
        return self._git("rev-parse", "--short", "HEAD").strip()

    def _last_changed(self) -> dict[str, float]:
        """When git last changed each file. One walk, cached until HEAD moves."""
        if not self.is_repo():
            return {}
        head = self._git("rev-parse", "-q", "--verify", "HEAD", check=False).strip()
        if not head:
            return {}
        if head != self._times_head:
            times: dict[str, float] = {}
            stamp = 0.0
            for line in self._git("log", "--format=%x00%ct", "--name-only", "--no-renames").splitlines():
                if line.startswith("\0"):
                    stamp = float(line[1:])
                elif line and line not in times:
                    times[line] = stamp
            self._times, self._times_head = times, head
        return self._times

    # ----- reading -----------------------------------------------------------

    def _files(self) -> Iterator[Path]:
        if not self.root.is_dir():
            return
        if self.is_repo():
            # Anything the store's .gitignore excludes is not memory: logs,
            # per-machine state, automatic snapshots.
            listed = self._git("ls-files", "-co", "--exclude-standard", "-z", check=False).split("\0")
            candidates = [self.root / item for item in sorted(set(listed)) if item.endswith(".md")]
        else:
            candidates = []
            for folder, dirs, files in os.walk(self.root):
                dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
                candidates += [Path(folder) / name for name in sorted(files) if name.endswith(".md")]
        for path in candidates:
            hidden = SKIP_DIRS.intersection(path.relative_to(self.root).parts)
            is_index = path.name == INDEX and path.parent == self.root
            if path.is_file() and not hidden and not is_index and ".conflict-" not in path.name:
                yield path

    def _read(self, path: Path, times: dict[str, float]) -> Optional[Note]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        rel = path.relative_to(self.root).as_posix()
        meta, body = parse_frontmatter(text)
        links: list[str] = []
        for raw in _WIKILINK.findall(body) + [Path(m).stem for m in _MDLINK.findall(body)]:
            target = raw.strip()
            if target and target not in links:
                links.append(target)
        modified = times.get(rel, 0.0)
        if meta.get("modified"):
            try:
                stamp = datetime.fromisoformat(meta["modified"].replace("Z", "+00:00"))
                stamp = stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
                modified = max(modified, stamp.timestamp())
            except ValueError:
                pass
        if not modified:
            try:
                modified = path.stat().st_mtime
            except OSError:
                modified = 0.0
        description = meta.get("description", "")
        if not description:
            for line in body.splitlines():
                line = line.strip().lstrip("#").strip()
                if len(line) > 20 and not line.startswith(("---", "_", "<!--")):
                    description = line[:160]
                    break
        kind = meta.get("type", "").lower()
        return Note(name=meta.get("name") or path.stem, path=rel, description=description,
                    type=kind if kind in TYPES else "", scope=meta.get("scope", "global"),
                    status=meta.get("status", "active"), source=meta.get("source", ""), modified=modified,
                    body=body, text=text, links=links, archived=rel.startswith(ARCHIVE + "/"))

    def _visible(self, note: Note) -> bool:
        return not (note.scope.startswith("machine:") and note.scope != f"machine:{self.host}")

    def notes(self, include_archived: bool = False) -> list[Note]:
        times = self._last_changed()
        found = [self._read(path, times) for path in self._files()]
        return [n for n in found if n and self._visible(n) and (include_archived or not n.archived)]

    @staticmethod
    def _by_name(notes: list[Note]) -> dict[str, Note]:
        table: dict[str, Note] = {}
        for note in notes:
            table.setdefault(Path(note.path).stem.lower(), note)
        for note in notes:
            table[note.name.lower()] = note
        return table

    def get(self, name: str, include_archived: bool = False) -> Optional[Note]:
        return self._by_name(self.notes(include_archived)).get(name.strip().lower())

    def _require(self, name: str, include_archived: bool = False) -> Note:
        note = self.get(name, include_archived)
        if note:
            return note
        hint = self.did_you_mean(name)
        raise MemoryError_(f"No memory called '{name}'." + (f" Did you mean: {', '.join(hint)}?" if hint else ""))

    def did_you_mean(self, name: str) -> list[str]:
        needle = name.strip().lower()
        names = [n.name for n in self.notes()]
        close = [n for n in names if needle and (needle in n.lower() or n.lower() in needle)]
        return (close or [r["name"] for r in self.search(name, limit=3)])[:5]

    def index_text(self) -> str:
        try:
            return (self.root / INDEX).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def _label(self, note: Note) -> str:
        parts = [f"last changed {note.age_days()} day(s) ago"]
        if note.source:
            parts.append(f"by {note.source}")
        if note.scope != "global":
            parts.append(f"scope {note.scope}")
        line = ", ".join(parts)
        return f"{line}. **May be out of date: check before relying on it.**" if note.stale() else line + "."

    def recall(self, name: Optional[str] = None, depth: int = 1, budget: int = 24_000) -> str:
        """The index, or one memory and the ones linked to it, up to a budget.

        Breadth-first so the nearest memories are read first. The limit is in
        characters, because ten short notes and one huge one are not the same
        amount of context.
        """
        if not (name or "").strip():
            text = self.index_text().strip()
            if not text:
                return "This memory is empty. Use `remember` to add to it."
            return ("# Memory index\n\nOne line per memory. Call `recall` with a name to read one and "
                    "the memories it links to.\n\n" + text)
        notes = self.notes()
        table = self._by_name(notes)
        start = table.get((name or "").strip().lower())
        if not start:
            start = self._require(name or "", include_archived=True)
            return f"# {start.name} (archived)\n\n_{self._label(start)}_\n\n{start.text.strip()}"
        neighbours: dict[str, list[Note]] = {n.path: [] for n in notes}
        for note in notes:
            for target in note.links:
                other = table.get(target.lower())
                if other and other.path != note.path:
                    neighbours[note.path].append(other)
                    neighbours[other.path].append(note)
        seen, order, frontier = {start.path}, [start], [start]
        for _ in range(max(0, min(depth, 3))):
            following: list[Note] = []
            for note in frontier:
                for other in sorted(neighbours[note.path], key=lambda n: n.name):
                    if other.path not in seen:
                        seen.add(other.path)
                        order.append(other)
                        following.append(other)
            frontier = following
        parts = [f"# Memory around `{start.name}`"]
        used, left_out = 0, []
        for note in order:
            if used + len(note.text) > budget and used:
                left_out.append(note.name)
                continue
            parts.append(f"\n---\n\n## {note.name}\n\n_{self._label(note)}_\n\n{note.text.strip()}")
            used += len(note.text)
        if left_out:
            parts.append(f"\n---\n\n_Left out to stay within budget: {', '.join(left_out)}. Recall one by name._")
        return "\n".join(parts)

    def _rank_factor(self, note: Note) -> float:
        return 0.6 if note.scope.startswith("project:") and note.scope != f"project:{self.project}" else 1.0

    def search(self, query: str, limit: int = 10, include_archived: bool = False) -> list[dict[str, Any]]:
        terms = [t for t in re.findall(r"[\w'-]+", query.lower()) if t not in _STOP] or re.findall(r"[\w'-]+", query.lower())
        if not terms:
            return []
        patterns = [re.compile(r"\b" + re.escape(t)) for t in terms]
        results = []
        for note in self.notes(include_archived):
            name, description, body = note.name.lower().replace("-", " "), note.description.lower(), note.body.lower()
            score, matched, first = 0.0, 0, -1
            for pattern in patterns:
                in_body = pattern.findall(body)
                hit_name, hit_description = bool(pattern.search(name)), bool(pattern.search(description))
                score += 6 * hit_name + 4 * hit_description + (min(3.0, 1 + math.log(len(in_body))) if in_body else 0)
                if hit_name or hit_description or in_body:
                    matched += 1
                    found = pattern.search(body)
                    if found and first < 0:
                        first = found.start()
            if not matched:
                continue
            score *= (matched / len(terms)) ** 2 * self._rank_factor(note)
            snippet = ""
            if first >= 0:
                snippet = " ".join(note.body[max(0, first - 60):first + 140].split())
            results.append(dict(note.summary(), score=round(score, 2), snippet=snippet))
        results.sort(key=lambda r: (-r["score"], -r["modified"]))
        return results[:limit]

    # ----- writing -----------------------------------------------------------

    @staticmethod
    def _check_secret(*texts: str) -> None:
        for text in texts:
            if text and SECRET_RE.search(text):
                raise MemoryError_("That looks like it contains a password, key or token, so it was not saved. "
                                   "Remove the secret and try again.")

    def _write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def _inside(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        root = self.root.resolve()
        if path != root and root not in path.parents:
            raise MemoryError_("That path is outside the memory folder.")
        return path

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def _via(source: Optional[str]) -> str:
        return f" (via {source})" if source else ""

    def _index_add(self, note_path: str, name: str, description: str) -> None:
        index = self.root / INDEX
        text = index.read_text(encoding="utf-8", errors="replace") if index.exists() else ""
        if f"({note_path})" in text:
            return
        title = name.replace("-", " ").replace("_", " ")
        title = title[:1].upper() + title[1:]
        line = f"- [{title}]({note_path}) — {' '.join(description.split())}\n"
        self._write(index, text + ("" if not text or text.endswith("\n") else "\n") + line)

    def _index_update(self, note_path: str, description: str) -> None:
        index = self.root / INDEX
        if not index.exists():
            return
        lines = index.read_text(encoding="utf-8", errors="replace").splitlines(True)
        for i, line in enumerate(lines):
            if f"({note_path})" in line:
                match = re.match(r"(.*?\(" + re.escape(note_path) + r"\))", line)
                if match:
                    lines[i] = f"{match.group(1)} — {' '.join(description.split())}\n"
        self._write(index, "".join(lines))

    def _index_remove(self, note_path: str) -> None:
        index = self.root / INDEX
        if not index.exists():
            return
        lines = index.read_text(encoding="utf-8", errors="replace").splitlines(True)
        self._write(index, "".join(l for l in lines if f"({note_path})" not in l))

    def remember(self, name: str, description: str, body: str, type: str = "project",
                 scope: Optional[str] = None, source: Optional[str] = None) -> dict[str, Any]:
        name = name.strip().lower().replace(" ", "-")
        if not _NAME_OK.match(name):
            raise MemoryError_("A memory name must be short, lowercase letters, numbers and hyphens, like 'prefers-tabs'.")
        if name == INDEX.lower().removesuffix(".md"):
            raise MemoryError_("That name is reserved for the index.")
        if type not in TYPES:
            raise MemoryError_(f"Type must be one of: {', '.join(TYPES)}.")
        if not description.strip() or not body.strip():
            raise MemoryError_("A memory needs a one-line description and a body.")
        self._check_secret(name, description, body)
        self._ensure_repo()
        with self.lock():
            if self.get(name, include_archived=True):
                raise MemoryError_(f"A memory called '{name}' already exists. Use `amend` to change it, or pick another name.")
            similar = [r["name"] for r in self.search(f"{name.replace('-', ' ')} {description}", limit=3) if r["score"] >= 12]
            relative = f"{name}.md"
            path = self._inside(relative)
            text = set_frontmatter(body.strip() + "\n", {"name": name, "description": " ".join(description.split())})
            updates: dict[str, Optional[str]] = {"type": type, "modified": self._now()}
            if scope and scope != "global":
                updates["scope"] = scope
            if source:
                updates["source"] = source
            self._write(path, set_frontmatter(text, updates))
            self._index_add(relative, name, description)
            commit = self._commit(f"remember: {name}{self._via(source)}", [relative, INDEX])
        return {"name": name, "path": relative, "commit": commit, "similar": similar, "index": self.index_health()}

    def amend(self, name: str, old: Optional[str] = None, new: Optional[str] = None, body: Optional[str] = None,
              description: Optional[str] = None, type: Optional[str] = None, scope: Optional[str] = None,
              source: Optional[str] = None) -> dict[str, Any]:
        if old is None and body is None and description is None and type is None and scope is None:
            raise MemoryError_("Say what to change: `old` and `new` text, a new `body`, or a new `description`, `type` or `scope`.")
        if type is not None and type not in TYPES:
            raise MemoryError_(f"Type must be one of: {', '.join(TYPES)}.")
        self._check_secret(new or "", body or "", description or "")
        self._ensure_repo()
        with self.lock():
            note = self._require(name)
            self._starting_point([note.path, INDEX])
            text = note.text
            if old is not None:
                count = text.count(old) if old else 0
                if count != 1:
                    raise MemoryError_(f"The text to replace was found {count} time(s) in '{note.name}'. It must match exactly once.")
                text = text.replace(old, new or "")
            if body is not None:
                match = _FRONTMATTER.match(text)
                text = (text[:match.end()] if match else "") + body.strip() + "\n"
            updates: dict[str, Optional[str]] = {"modified": self._now()}
            if description is not None:
                updates["description"] = " ".join(description.split())
            if type is not None:
                updates["type"] = type
            if scope is not None:
                updates["scope"] = None if scope == "global" else scope
            if source:
                updates["source"] = source
            self._write(self._inside(note.path), set_frontmatter(text, updates))
            if description is not None:
                self._index_update(note.path, description)
            commit = self._commit(f"amend: {note.name}{self._via(source)}", [note.path, INDEX])
        return {"name": note.name, "path": note.path, "commit": commit}

    def archive(self, name: str, reason: Optional[str] = None, source: Optional[str] = None) -> dict[str, Any]:
        """Forgetting moves a memory out of the way. It is never deleted."""
        self._ensure_repo()
        with self.lock():
            note = self._require(name)
            self._starting_point([note.path, INDEX])
            destination = f"{ARCHIVE}/{note.path}"
            counter = 2
            while (self.root / destination).exists():
                destination = f"{ARCHIVE}/{Path(note.path).with_suffix('')}-{counter}.md"
                counter += 1
            target = self._inside(destination)
            target.parent.mkdir(parents=True, exist_ok=True)
            source_path = self._inside(note.path)
            updates: dict[str, Optional[str]] = {"status": "archived", "modified": self._now()}
            if reason:
                updates["archived_reason"] = " ".join(reason.split())
            self._write(target, set_frontmatter(note.text, updates))
            source_path.unlink()
            self._index_remove(note.path)
            commit = self._commit(f"archive: {note.name}{self._via(source)}", [note.path, destination, INDEX])
        return {"name": note.name, "path": destination, "commit": commit}

    def restore(self, name: str, source: Optional[str] = None) -> dict[str, Any]:
        self._ensure_repo()
        with self.lock():
            note = self._require(name, include_archived=True)
            if not note.archived:
                raise MemoryError_(f"'{note.name}' is not archived.")
            relative = note.path[len(ARCHIVE) + 1:]
            if (self.root / relative).exists():
                raise MemoryError_(f"Something already exists at {relative}.")
            self._write(self._inside(relative), set_frontmatter(note.text, {"status": None, "archived_reason": None, "modified": self._now()}))
            self._inside(note.path).unlink()
            self._index_add(relative, note.name, note.description)
            commit = self._commit(f"restore: {note.name}{self._via(source)}", [note.path, relative, INDEX])
        return {"name": note.name, "path": relative, "commit": commit}

    # ----- history -----------------------------------------------------------

    def changes(self, limit: int = 30) -> list[dict[str, Any]]:
        if not self.is_repo() or not self._git("rev-parse", "-q", "--verify", "HEAD", check=False).strip():
            return []
        out = self._git("log", "-n", str(max(1, min(limit, 500))), "--format=%x1e%H%x1f%P%x1f%ct%x1f%s", "--name-status", "--no-renames")
        reviewed = self.reviewed()
        seen_reviewed = False
        results = []
        for chunk in out.split("\x1e")[1:]:
            head, _, rest = chunk.partition("\n")
            commit, parents, stamp, subject = head.split("\x1f", 3)
            if commit == reviewed:
                seen_reviewed = True
            if subject == STARTING_POINT:
                continue
            files = []
            for line in rest.splitlines():
                bits = line.split("\t")
                if len(bits) == 2 and bits[1] not in HIDDEN_IN_CHANGES:
                    files.append({"status": bits[0][0], "path": bits[1]})
            if not files and len(parents.split()) < 2:
                continue
            via = re.search(r"\(via ([^)]+)\)", subject)
            sync = re.search(r" from (\S+) \d{4}-", subject)
            results.append({"hash": commit, "short": commit[:8], "when": int(stamp), "subject": subject, "files": files,
                            "who": via.group(1) if via else (sync.group(1) if sync else ""),
                            "merge": len(parents.split()) > 1, "reviewed": seen_reviewed,
                            "can_undo": bool(parents.strip()) and len(parents.split()) == 1})
        return results

    def _commit_arg(self, value: str) -> str:
        if not _HASH_OK.match(value or ""):
            raise MemoryError_("That is not a change id.")
        full = self._git("rev-parse", "-q", "--verify", f"{value}^{{commit}}", check=False).strip()
        if not full:
            raise MemoryError_("No change with that id.")
        return full

    def _file_arg(self, value: str) -> str:
        self._inside(value)
        return value

    def diff(self, commit: str, file: Optional[str] = None) -> str:
        args = ["show", "--format=", "--patch", self._commit_arg(commit)]
        if file:
            args += ["--", self._file_arg(file)]
        return self._git(*args)[:200_000]

    def history(self, name: str) -> list[dict[str, Any]]:
        note = self._require(name, include_archived=True)
        if not self.is_repo():
            return []
        out = self._git("log", "-n", "50", "--follow", "--format=%H%x1f%ct%x1f%s", "--", note.path, check=False)
        return [dict(zip(("hash", "when", "subject"), line.split("\x1f", 2))) for line in out.splitlines() if line]

    def undo(self, commit: Optional[str] = None, file: Optional[str] = None) -> dict[str, Any]:
        """Reverse one change, as a new change, so the undo can itself be undone."""
        if not self.is_repo():
            raise MemoryError_("There is no history to undo yet.")
        with self.lock():
            if commit is None:
                candidates = [c for c in self.changes(20) if c["can_undo"]]
                if not candidates:
                    raise MemoryError_("There is nothing to undo.")
                target = candidates[0]["hash"]
            else:
                target = self._commit_arg(commit)
            parents = self._git("rev-list", "--parents", "-n", "1", target).split()[1:]
            if len(parents) != 1:
                raise MemoryError_("That change is a merge or the very first change, so it can't be undone on its own.")
            short = target[:8]
            if file:
                relative = self._file_arg(file)
                patch = self._git("diff", "--binary", "--no-renames", parents[0], target, "--", relative)
                if not patch.strip():
                    raise MemoryError_(f"That change did not touch {relative}.")
                applied = subprocess.run(["git", "-C", str(self.root), "apply", "-R", "--index"], input=patch, text=True,
                                         capture_output=True, encoding="utf-8", timeout=30)
                if applied.returncode != 0:
                    raise MemoryError_(f"{relative} has changed since then, so this can't be undone automatically.")
                self._git("commit", "-q", "-m", f"undo: {relative} from {short}")
            else:
                reverted = subprocess.run(["git", "-C", str(self.root), "revert", "--no-edit", target], capture_output=True,
                                          text=True, encoding="utf-8", timeout=30)
                if reverted.returncode != 0:
                    subprocess.run(["git", "-C", str(self.root), "revert", "--abort"], capture_output=True, timeout=30)
                    raise MemoryError_("Those files have changed since then, so this can't be undone automatically.")
            return {"undid": short, "file": file, "commit": self._git("rev-parse", "--short", "HEAD").strip()}

    def reviewed(self) -> str:
        try:
            return (self.root / ".git" / "memsync" / "reviewed").read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def mark_reviewed(self, commit: Optional[str] = None) -> str:
        full = self._commit_arg(commit) if commit else self._git("rev-parse", "HEAD").strip()
        state = self.root / ".git" / "memsync"
        state.mkdir(parents=True, exist_ok=True)
        (state / "reviewed").write_text(full + "\n", encoding="utf-8")
        return full

    # ----- health ------------------------------------------------------------

    def index_health(self) -> dict[str, Any]:
        text = self.index_text()
        lines, size = len(text.splitlines()), len(text.encode("utf-8"))
        return {"lines": lines, "bytes": size, "max_lines": INDEX_MAX_LINES, "max_bytes": INDEX_MAX_BYTES,
                "over": lines > INDEX_MAX_LINES or size > INDEX_MAX_BYTES,
                "near": lines > INDEX_MAX_LINES * 0.85 or size > INDEX_MAX_BYTES * 0.85}

    def graph(self) -> dict[str, Any]:
        notes = self.notes()
        table = self._by_name(notes)
        edges, missing = [], {}
        for note in notes:
            for target in note.links:
                other = table.get(target.lower())
                if other and other.path != note.path:
                    edges.append({"from": note.name, "to": other.name})
                elif not other:
                    missing[target] = missing.get(target, 0) + 1
        linked = {e["from"] for e in edges} | {e["to"] for e in edges}
        nodes = [dict(n.summary(), orphan=n.name not in linked) for n in notes]
        return {"nodes": nodes, "edges": edges, "missing": missing}

    def tidy(self) -> dict[str, Any]:
        """What needs attention. It reports and never changes anything."""
        notes = self.notes()
        index = self.index_text()
        index_paths = set(re.findall(r"\]\(([^)\s]+?\.md)\)", index))
        by_path = {n.path for n in notes}
        def signature(note: Note) -> set:
            return {t for t in re.findall(r"[a-z0-9]+", note.description.lower()) if t not in _STOP and len(t) > 2}
        signatures = [(n, signature(n)) for n in notes]
        duplicates = []
        for i, (a, sa) in enumerate(signatures):
            for b, sb in signatures[i + 1:]:
                if len(sa) >= 3 and len(sb) >= 3 and len(sa & sb) / len(sa | sb) >= 0.75:
                    duplicates.append([a.name, b.name])
        graph = self.graph()
        conflict_copies = []
        for folder, dirs, files in os.walk(self.root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            conflict_copies += [(Path(folder) / f).relative_to(self.root).as_posix() for f in files if ".conflict-" in f]
        report = {
            "index": self.index_health(),
            "not_in_index": sorted(n.path for n in notes if n.path not in index_paths and n.path.count("/") == 0),
            "index_points_nowhere": sorted(p for p in index_paths if p not in by_path and not (self.root / p).exists()),
            "duplicates": duplicates,
            "stale": [{"name": n.name, "age_days": n.age_days(), "type": n.type} for n in sorted(notes, key=lambda n: n.modified) if n.stale()],
            "conflict_copies": sorted(conflict_copies),
            "oversized": [{"name": n.name, "chars": len(n.text)} for n in notes if len(n.text) > 20_000],
            "orphans": sorted(n["name"] for n in graph["nodes"] if n["orphan"] and n["path"] not in index_paths),
            "wanted": graph["missing"],
        }
        report["issues"] = (int(report["index"]["over"]) + len(report["not_in_index"]) + len(report["index_points_nowhere"])
                            + len(duplicates) + len(report["stale"]) + len(conflict_copies))
        return report

    def stats(self) -> dict[str, Any]:
        notes = self.notes()
        return {"memories": len(notes), "archived": len(self.notes(True)) - len(notes),
                "by_type": {t: sum(n.type == t for n in notes) for t in TYPES},
                "stale": sum(n.stale() for n in notes), "index": self.index_health()}

