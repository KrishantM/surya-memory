"""Tests for the store, the MCP server, one-store setup and tool connections.

Everything runs against a throwaway HOME. Standard library only.
Run: python3 -m unittest discover -s tests -v
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from surya_memory import connect, onestore, server  # noqa: E402
from surya_memory.store import MemoryError_, Store, parse_frontmatter, resolve_store, set_frontmatter  # noqa: E402

CLI = [sys.executable, str(ROOT / "scripts" / "surya-memory")]


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name).resolve()
        clear = ("SURYA_MEMORY_DIR", "MEMSYNC_DIR", "SURYA_MEMORY_HOME", "CODEX_HOME", "CLAUDE_PROJECT_DIR")
        env = {k: v for k, v in os.environ.items() if k not in clear}
        env.update(HOME=str(self.home), MEMSYNC_HOST="mac", GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        self.root = self.home / "store"
        self.store = Store(self.root, project="alpha", host="mac")

    def add(self, name="prefers-tabs", description="Uses tabs in every language", body="Always tabs.", **kw):
        return self.store.remember(name, description, body, **kw)

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, text=True).stdout


class Frontmatter(Sandbox):
    def test_reads_the_format_claude_code_writes(self):
        meta, body = parse_frontmatter('---\nname: a\ndescription: "x: y"\nmetadata:\n  type: user\n---\n\nHello\n')
        self.assertEqual((meta["name"], meta["description"], meta["type"]), ("a", "x: y", "user"))
        self.assertEqual(body, "\nHello\n")

    def test_no_frontmatter(self):
        self.assertEqual(parse_frontmatter("# Title\n"), ({}, "# Title\n"))

    def test_update_leaves_other_lines_alone(self):
        text = "---\nname: a\nmetadata:\n  type: user\n  originSessionId: abc\n---\nBody\n"
        updated = set_frontmatter(text, {"scope": "project:x", "description": "d"})
        self.assertIn("  originSessionId: abc", updated)
        self.assertIn("  scope: project:x".replace("project:x", '"project:x"'), updated)
        self.assertEqual(parse_frontmatter(updated)[0]["scope"], "project:x")
        self.assertTrue(updated.endswith("---\nBody\n"))

    def test_top_level_key_not_confused_with_nested(self):
        text = "---\nname: a\nmetadata:\n  name: nested\n---\nB\n"
        updated = set_frontmatter(text, {"name": "b"})
        self.assertIn("name: b\n", updated)
        self.assertIn("  name: nested", updated)

    def test_remove_a_field(self):
        text = "---\nname: a\nmetadata:\n  status: archived\n---\nB\n"
        self.assertNotIn("status", set_frontmatter(text, {"status": None}))


class Writing(Sandbox):
    def test_remember_writes_note_index_and_commit(self):
        result = self.add(source="codex")
        self.assertTrue((self.root / "prefers-tabs.md").is_file())
        self.assertIn("(prefers-tabs.md) — Uses tabs", self.store.index_text())
        self.assertIn("remember: prefers-tabs (via codex)", self.git("log", "-1", "--format=%s"))
        self.assertTrue(result["commit"])
        meta, _ = parse_frontmatter((self.root / "prefers-tabs.md").read_text())
        self.assertEqual((meta["type"], meta["source"]), ("project", "codex"))

    def test_never_overwrites(self):
        self.add()
        with self.assertRaises(MemoryError_):
            self.add(body="different")
        self.assertIn("Always tabs.", (self.root / "prefers-tabs.md").read_text())

    def test_name_cannot_escape_the_folder(self):
        for bad in ("../evil", "/etc/passwd", "a/b", "..", ""):
            with self.assertRaises(MemoryError_, msg=bad):
                self.add(name=bad)
        self.assertFalse((self.home / "evil.md").exists())

    def test_refuses_secrets(self):
        token = "ghp_" + "a" * 36
        with self.assertRaises(MemoryError_) as caught:
            self.add(body=f"my token is {token}")
        self.assertNotIn(token, str(caught.exception))
        self.assertFalse((self.root / "prefers-tabs.md").exists())
        self.add()
        with self.assertRaises(MemoryError_):
            self.store.amend("prefers-tabs", old="Always tabs.", new=token)

    def test_flags_similar_memories(self):
        self.add(name="prefers-tabs-always", description="User prefers tabs for indentation")
        result = self.add(name="tabs-preference", description="User prefers tabs for indentation")
        self.assertIn("prefers-tabs-always", result["similar"])

    def test_amend_exact_text_once(self):
        self.add(body="one two two")
        with self.assertRaises(MemoryError_):
            self.store.amend("prefers-tabs", old="two", new="three")
        self.store.amend("prefers-tabs", old="one", new="uno")
        self.assertIn("uno two two", (self.root / "prefers-tabs.md").read_text())

    def test_amend_description_updates_index(self):
        self.add()
        self.store.amend("prefers-tabs", description="Now spaces")
        self.assertIn("(prefers-tabs.md) — Now spaces", self.store.index_text())
        self.assertNotIn("Uses tabs", self.store.index_text())

    def test_amend_keeps_frontmatter(self):
        self.add()
        self.store.amend("prefers-tabs", body="New body.")
        note = self.store.get("prefers-tabs")
        self.assertEqual((note.body.strip(), note.description), ("New body.", "Uses tabs in every language"))

    def test_archive_moves_and_restore_returns(self):
        self.add()
        self.store.archive("prefers-tabs", reason="changed mind")
        self.assertFalse((self.root / "prefers-tabs.md").exists())
        self.assertTrue((self.root / "archive/prefers-tabs.md").is_file())
        self.assertNotIn("prefers-tabs", self.store.index_text())
        self.assertIsNone(self.store.get("prefers-tabs"))
        self.assertEqual(self.store.get("prefers-tabs", True).status, "archived")
        self.store.restore("prefers-tabs")
        self.assertTrue((self.root / "prefers-tabs.md").is_file())
        self.assertIn("prefers-tabs", self.store.index_text())
        self.assertEqual(self.store.get("prefers-tabs").status, "active")

    def test_archived_name_still_taken(self):
        self.add()
        self.store.archive("prefers-tabs")
        with self.assertRaises(MemoryError_):
            self.add()

    def test_archive_a_file_git_has_not_seen(self):
        self.add()
        (self.root / "native.md").write_text("---\nname: native\ndescription: from native tools\n---\nBody long enough\n")
        self.store.archive("native")
        self.assertTrue((self.root / "archive/native.md").is_file())


class Reading(Sandbox):
    def setUp(self):
        super().setUp()
        self.add("alpha-note", "About the alpha system", "See [[beta-note]] for more.")
        self.add("beta-note", "About the beta system", "Links on to [[gamma-note]].")
        self.add("gamma-note", "About gamma", "End of the chain. Redis cache lives here.")
        self.add("lonely", "Nothing links here", "Standing alone with the word cache once.")

    def test_gitignored_files_are_not_memory(self):
        (self.root / ".gitignore").write_text("snapshots/\n")
        (self.root / "snapshots").mkdir()
        (self.root / "snapshots/old.md").write_text("---\nname: old\ndescription: snapshot copy\n---\nBody text\n")
        (self.root / "fresh.md").write_text("---\nname: fresh\ndescription: not committed yet\n---\nBody text\n")
        names = [n.name for n in self.store.notes()]
        self.assertNotIn("old", names)
        self.assertIn("fresh", names)

    def test_recall_without_a_name_gives_the_index(self):
        self.assertIn("(alpha-note.md)", self.store.recall())

    def test_recall_follows_links_and_backlinks(self):
        text = self.store.recall("beta-note")
        for name in ("beta-note", "alpha-note", "gamma-note"):
            self.assertIn(f"## {name}", text)
        self.assertNotIn("## lonely", text)

    def test_recall_depth(self):
        self.assertNotIn("## gamma-note", self.store.recall("alpha-note", depth=1))
        self.assertIn("## gamma-note", self.store.recall("alpha-note", depth=2))

    def test_recall_respects_budget(self):
        self.store.amend("gamma-note", body="x" * 5000)
        text = self.store.recall("beta-note", budget=2000)
        self.assertIn("Left out to stay within budget", text)
        self.assertIn("## beta-note", text)

    def test_recall_unknown_suggests(self):
        with self.assertRaises(MemoryError_) as caught:
            self.store.recall("alpha")
        self.assertIn("alpha-note", str(caught.exception))

    def test_recall_finds_archived_directly(self):
        self.store.archive("lonely")
        self.assertIn("(archived)", self.store.recall("lonely"))

    def test_search_ranks_name_over_body(self):
        names = [r["name"] for r in self.store.search("gamma")]
        self.assertEqual(names[0], "gamma-note")
        self.assertIn("beta-note", names)

    def test_search_prefers_all_terms(self):
        self.assertEqual(self.store.search("redis cache")[0]["name"], "gamma-note")

    def test_search_word_start_only(self):
        self.assertEqual(self.store.search("ache"), [])

    def test_search_excludes_archive_unless_asked(self):
        self.store.archive("lonely")
        self.assertNotIn("lonely", [r["name"] for r in self.store.search("standing alone")])
        self.assertIn("lonely", [r["name"] for r in self.store.search("standing alone", include_archived=True)])

    def test_other_machine_notes_hidden(self):
        self.add("win-path", "Windows only path", "C drive", scope="machine:win")
        self.add("mac-path", "Mac only path", "home", scope="machine:mac")
        names = [n.name for n in self.store.notes()]
        self.assertNotIn("win-path", names)
        self.assertIn("mac-path", names)

    def test_other_project_ranks_lower(self):
        self.add("deploy-mine", "Deploy steps", "deploy", scope="project:alpha")
        self.add("deploy-other", "Deploy steps", "deploy", scope="project:other")
        self.assertEqual([r["name"] for r in self.store.search("deploy steps")][:2], ["deploy-mine", "deploy-other"])

    def test_old_project_memory_is_flagged_but_preferences_are_not(self):
        self.add("old-state", "Old state", "b", type="project")
        self.add("old-pref", "Old pref", "b", type="feedback")
        for name in ("old-state", "old-pref"):
            path = self.root / f"{name}.md"
            path.write_text(set_frontmatter(path.read_text(), {"modified": "2020-01-01T00:00:00Z"}))
        store = self.store
        with mock.patch.object(store, "_last_changed", return_value={}):
            self.assertTrue(store.get("old-state").stale())
            self.assertFalse(store.get("old-pref").stale())
            self.assertIn("check before relying", store.recall("old-state"))


class History(Sandbox):
    def test_changes_shows_who_and_what(self):
        self.add(source="codex")
        change = self.store.changes()[0]
        self.assertEqual(change["who"], "codex")
        self.assertIn("prefers-tabs.md", [f["path"] for f in change["files"]])

    def test_undo_latest_change(self):
        self.add()
        self.add("second", "Second memory", "body")
        self.store.undo()
        self.assertFalse((self.root / "second.md").exists())
        self.assertNotIn("second", self.store.index_text())
        self.assertTrue((self.root / "prefers-tabs.md").exists())

    def test_undo_can_be_undone(self):
        self.add()
        self.add("second", "Second memory", "body")
        self.store.undo()
        self.store.undo()
        self.assertTrue((self.root / "second.md").exists())

    def test_undo_one_file_of_a_change(self):
        self.add()
        self.add("second", "Second memory", "body")
        commit = self.store.changes()[0]["hash"]
        self.store.undo(commit, "second.md")
        self.assertFalse((self.root / "second.md").exists())
        self.assertIn("second", self.store.index_text())

    def test_undo_refuses_when_changed_since(self):
        self.add()
        self.store.amend("prefers-tabs", old="Always tabs.", new="Sometimes tabs.")
        commit = self.store.changes()[0]["hash"]
        self.store.amend("prefers-tabs", old="Sometimes tabs.", new="Never tabs.")
        with self.assertRaises(MemoryError_):
            self.store.undo(commit)
        self.assertIn("Never tabs.", (self.root / "prefers-tabs.md").read_text())
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_bad_ids_rejected(self):
        self.add()
        for bad in ("HEAD", "--all", "main", "zzzzzzz"):
            with self.assertRaises(MemoryError_, msg=bad):
                self.store.undo(bad)
            with self.assertRaises(MemoryError_, msg=bad):
                self.store.diff(bad)

    def test_diff_cannot_read_outside(self):
        self.add()
        with self.assertRaises(MemoryError_):
            self.store.diff(self.store.changes()[0]["hash"], "../../etc/passwd")

    def test_reviewed_marker(self):
        self.add()
        self.add("second", "Second memory", "body")
        self.store.mark_reviewed(self.store.changes()[1]["hash"])
        changes = self.store.changes()
        self.assertEqual([c["reviewed"] for c in changes[:2]], [False, True])

    def test_write_waits_for_sync_lock(self):
        self.add()
        lock = self.root / ".git/memsync/lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.mkdir()
        old = time.time() - 300
        os.utime(lock, (old, old))  # a dead sync's lock is cleared
        self.add("second", "Second memory", "body")
        self.assertFalse(lock.exists())


class Tidy(Sandbox):
    def test_reports_problems(self):
        self.add("real", "Something real here", "body [[wanted-note]]")
        self.add("twin-one", "User deploys through vercel previews", "b")
        self.add("twin-two", "User deploys through vercel previews always", "b")
        (self.root / "not-listed.md").write_text("---\nname: not-listed\ndescription: missing from index\n---\nBody text here\n")
        (self.root / "x.conflict-mac-1.md").write_text("copy")
        with (self.root / "MEMORY.md").open("a") as handle:
            handle.write("- [Gone](gone.md) — no file\n")
        report = self.store.tidy()
        self.assertIn("not-listed.md", report["not_in_index"])
        self.assertIn("gone.md", report["index_points_nowhere"])
        self.assertIn(["twin-one", "twin-two"], report["duplicates"])
        self.assertIn("x.conflict-mac-1.md", report["conflict_copies"])
        self.assertEqual(report["wanted"], {"wanted-note": 1})

    def test_index_limits_match_claude_code(self):
        self.add()
        (self.root / "MEMORY.md").write_text("- line\n" * 250)
        self.assertTrue(self.store.index_health()["over"])
        self.assertGreater(self.store.tidy()["issues"], 0)


class StartingPoint(Sandbox):
    """Memory brought in from elsewhere has no history until something changes it."""

    def imported(self, name="imported", text="The original words."):
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / f"{name}.md").write_text(f"---\nname: {name}\ndescription: Came from elsewhere\nmetadata:\n  type: project\n---\n\n{text}\n")

    def test_undoing_the_first_edit_brings_back_the_original(self):
        self.imported()
        self.store.amend("imported", old="The original words.", new="New words.")
        self.assertIn("New words.", (self.root / "imported.md").read_text())
        self.store.undo()
        self.assertIn("The original words.", (self.root / "imported.md").read_text())

    def test_undoing_the_first_archive_brings_it_back(self):
        self.imported()
        self.store.archive("imported")
        self.assertFalse((self.root / "imported.md").exists())
        self.store.undo()
        self.assertIn("The original words.", (self.root / "imported.md").read_text())

    def test_a_file_added_by_hand_later_can_also_be_undone(self):
        self.add()
        self.imported("by-hand", "Typed in an editor.")
        self.store.amend("by-hand", old="Typed in an editor.", new="Changed by AI.")
        self.store.undo()
        self.assertIn("Typed in an editor.", (self.root / "by-hand.md").read_text())

    def test_starting_point_is_not_a_change_and_cannot_be_undone(self):
        self.imported()
        self.store.amend("imported", old="The original words.", new="New.")
        self.assertEqual([c["subject"].split(":")[0] for c in self.store.changes()], ["amend"])
        root = self.git("rev-list", "--max-parents=0", "HEAD").strip()
        with self.assertRaises(MemoryError_):
            self.store.undo(root)

    def test_a_credential_stays_out_of_the_starting_point(self):
        self.imported("leaky", "ghp_" + "a1B2" * 9)
        self.imported()
        self.store.amend("imported", old="The original words.", new="New.")
        self.assertNotIn("leaky.md", self.git("ls-files"))

    def test_changing_a_note_with_a_credential_is_refused(self):
        self.imported()
        self.store.amend("imported", old="The original words.", new="New.")
        self.imported("leaky", "ghp_" + "a1B2" * 9)
        with self.assertRaises(MemoryError_):
            self.store.archive("leaky")
        self.assertTrue((self.root / "leaky.md").exists())

    def test_sync_can_be_turned_on_afterwards(self):
        self.imported()
        self.store.amend("imported", old="The original words.", new="New.")
        remote = self.home / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
        subprocess.run(["bash", str(ROOT / "scripts/setup.sh"), "--dir", str(self.root), "--remote", str(remote)], check=True, capture_output=True)
        listed = subprocess.run(["git", "-C", str(remote), "ls-tree", "--name-only", "main"], capture_output=True, text=True).stdout
        self.assertIn("imported.md", listed)


class StablePython(Sandbox):
    def test_prefers_the_name_on_path_over_one_exact_version(self):
        with mock.patch.object(connect.sys, "executable", "/opt/pkg/python@3.14/bin/python3.14"), mock.patch.object(connect.shutil, "which", return_value="/usr/bin/python3"):
            self.assertEqual(connect.stable_python(), "/usr/bin/python3")

    def test_skips_a_virtualenv(self):
        with mock.patch.dict(os.environ, {"VIRTUAL_ENV": "/work/.venv"}), mock.patch.object(connect.shutil, "which", return_value="/work/.venv/bin/python3"):
            self.assertEqual(connect.stable_python(), "/usr/bin/python3")

    def test_falls_back_when_nothing_else_runs(self):
        with mock.patch.object(connect.shutil, "which", return_value=None), mock.patch.object(connect.subprocess, "run", side_effect=OSError):
            self.assertEqual(connect.stable_python(), sys.executable)


class Resolve(Sandbox):
    def test_order(self):
        self.assertIn(".claude/projects", str(resolve_store()))
        settings = self.home / ".claude/settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"autoMemoryDirectory": "~/from-settings"}))
        self.assertEqual(resolve_store(), self.home / "from-settings")
        onestore.pin_store(self.home / "pinned")
        self.assertEqual(resolve_store(), self.home / "pinned")
        os.environ["SURYA_MEMORY_DIR"] = str(self.home / "env")
        self.assertEqual(resolve_store(), self.home / "env")
        self.assertEqual(resolve_store(str(self.home / "arg")), self.home / "arg")

    def test_an_unset_plugin_option_is_ignored(self):
        with mock.patch.dict(os.environ, {"SURYA_MEMORY_DIR": "${user_config.memory_dir}"}):
            self.assertIn(".claude/projects", str(resolve_store()))
        with mock.patch.dict(os.environ, {"SURYA_MEMORY_DIR": ""}):
            self.assertIn(".claude/projects", str(resolve_store()))

    def test_empty_env_is_ignored(self):
        os.environ["SURYA_MEMORY_DIR"] = ""
        onestore.pin_store(self.home / "pinned")
        self.assertEqual(resolve_store(), self.home / "pinned")

    def test_shell_and_python_agree(self):
        onestore.pin_store(self.home / "pinned")
        script = f'source "{ROOT}/scripts/lib.sh"; memory_dir'
        out = subprocess.run(["bash", "-c", script], capture_output=True, text=True).stdout
        self.assertEqual(out, str(self.home / "pinned"))


class OneStore(Sandbox):
    def project(self, slug, cwd, files, index=None):
        folder = self.home / ".claude/projects" / slug
        (folder / "memory").mkdir(parents=True)
        (folder / "s.jsonl").write_text(json.dumps({"cwd": cwd}) + "\n")
        for name, text in files.items():
            path = folder / "memory" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        if index:
            (folder / "memory/MEMORY.md").write_text(index)
        return folder / "memory"

    @staticmethod
    def note(name, kind="project", body="Body"):
        return f"---\nname: {name}\ndescription: about {name}\nmetadata:\n  type: {kind}\n---\n{body}\n"

    def test_labels_come_from_the_real_folder_name(self):
        self.project("-a-my-cool-app", "/a/my-cool-app", {"x.md": self.note("x")})
        self.assertEqual(onestore.discover(self.root)[0].label, "my-cool-app")

    def test_merges_folders_and_keeps_both_on_a_clash(self):
        one = self.project("-a-one", "/a/one", {"same.md": self.note("same", body="One"), "a.md": self.note("a"), "me.md": self.note("me", "user")},
                           "- [Same](same.md) — x\n- [A](a.md) — y\n")
        two = self.project("-a-two", "/a/two", {"same.md": self.note("same", body="Two")}, "- [Same](same.md) — two\n")
        before = (two / "same.md").read_text()
        plan = onestore.make_plan(self.root)
        onestore.apply_plan(plan)
        self.assertIn("One", (self.root / "same.md").read_text())
        renamed = self.root / "same--two.md"
        self.assertIn("Two", renamed.read_text())
        self.assertEqual(parse_frontmatter(renamed.read_text())[0]["name"], "same--two")
        index = (self.root / "MEMORY.md").read_text()
        self.assertIn("(same--two.md)", index)
        self.assertIn("## From one", index)
        self.assertEqual((two / "same.md").read_text(), before)
        self.assertTrue((one / "a.md").exists())

    def test_people_facts_stay_global_project_facts_get_a_scope(self):
        self.project("-a-one", "/a/one", {"a.md": self.note("a"), "me.md": self.note("me", "user")})
        self.project("-a-two", "/a/two", {"b.md": self.note("b")})
        onestore.apply_plan(onestore.make_plan(self.root))
        self.assertEqual(parse_frontmatter((self.root / "a.md").read_text())[0]["scope"], "project:one")
        self.assertNotIn("scope", parse_frontmatter((self.root / "me.md").read_text())[0])

    def test_single_project_needs_no_scopes(self):
        self.project("-a-one", "/a/one", {"a.md": self.note("a")}, "- [A](a.md) — y\n")
        onestore.apply_plan(onestore.make_plan(self.root))
        self.assertNotIn("scope", (self.root / "a.md").read_text())
        self.assertEqual((self.root / "MEMORY.md").read_text(), "- [A](a.md) — y\n")

    def test_running_twice_changes_nothing(self):
        self.project("-a-one", "/a/one", {"same.md": self.note("same", body="One")}, "- [S](same.md) — x\n")
        self.project("-a-two", "/a/two", {"same.md": self.note("same", body="Two")}, "- [S](same.md) — y\n")
        onestore.apply_plan(onestore.make_plan(self.root))
        snapshot = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        plan = onestore.make_plan(self.root)
        self.assertEqual(plan.copies, [])
        onestore.apply_plan(plan)
        self.assertEqual({p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}, snapshot)

    def test_skips_git_internals_and_conflict_copies(self):
        memory = self.project("-a-one", "/a/one", {"a.md": self.note("a"), "b.conflict-mac-1.md": "x"})
        (memory / ".git").mkdir()
        (memory / ".git/config").write_text("secret")
        onestore.apply_plan(onestore.make_plan(self.root))
        self.assertFalse((self.root / ".git/config").exists())
        self.assertFalse((self.root / "b.conflict-mac-1.md").exists())

    def test_pin_store_backs_up_and_keeps_other_settings(self):
        settings = self.home / ".claude/settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"model": "x"}))
        onestore.pin_store(self.root)
        data = json.loads(settings.read_text())
        self.assertEqual((data["model"], data["autoMemoryDirectory"]), ("x", str(self.root)))
        self.assertEqual(len(list((self.home / ".surya-memory/backups").iterdir())), 1)

    def test_setup_shows_a_plan_and_changes_nothing_without_yes(self):
        self.project("-a-one", "/a/one", {"a.md": self.note("a")})
        out = subprocess.run(CLI + ["setup", "--no-sync", "--no-connect"], capture_output=True, text=True)
        self.assertIn("Nothing has changed yet", out.stdout)
        self.assertFalse((self.home / "agent-memory").exists())
        self.assertFalse((self.home / ".surya-memory").exists())

    def test_setup_yes_then_sync_finds_the_store(self):
        self.project("-a-one", "/a/one", {"a.md": self.note("a")})
        out = subprocess.run(CLI + ["setup", "--no-sync", "--no-connect", "--yes"], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue((self.home / "agent-memory/a.md").is_file())
        self.assertEqual(json.loads((self.home / ".claude/settings.json").read_text())["autoMemoryDirectory"], str(self.home / "agent-memory"))


class Connect(Sandbox):
    def test_json_tool_keeps_other_servers_and_is_repeatable(self):
        path = self.home / ".cursor/mcp.json"
        path.parent.mkdir()
        path.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "keep": 1}))
        connect.connect(["cursor"])
        connect.connect(["cursor"])
        data = json.loads(path.read_text())
        self.assertEqual(sorted(data["mcpServers"]), ["other", "surya-memory"])
        self.assertEqual(data["keep"], 1)
        self.assertEqual(len(list((self.home / ".surya-memory/backups").iterdir())), 1)
        connect.disconnect(["cursor"])
        self.assertEqual(sorted(json.loads(path.read_text())["mcpServers"]), ["other"])

    def test_bad_json_left_alone(self):
        path = self.home / ".cursor/mcp.json"
        path.parent.mkdir()
        path.write_text("{ not json")
        result = connect.connect(["cursor"])[0]
        self.assertFalse(result["ok"])
        self.assertEqual(path.read_text(), "{ not json")

    def test_codex_block_and_pointer(self):
        os.environ["CODEX_HOME"] = str(self.home / "codex")
        (self.home / "codex").mkdir()
        config = self.home / "codex/config.toml"
        config.write_text('model = "x"\n\n[projects."/a"]\ntrust_level = "trusted"\n')
        (self.home / "codex/AGENTS.md").write_text("# Mine\n")
        connect.connect(["codex"])
        connect.connect(["codex"])
        text = config.read_text()
        self.assertEqual(text.count("[mcp_servers.surya-memory]"), 1)
        self.assertIn('trust_level = "trusted"', text)
        agents = (self.home / "codex/AGENTS.md").read_text()
        self.assertTrue(agents.startswith("# Mine\n"))
        self.assertEqual(agents.count("surya-memory:start"), 1)
        connect.disconnect(["codex"])
        self.assertNotIn("surya-memory", config.read_text())
        self.assertIn('trust_level = "trusted"', config.read_text())
        self.assertEqual((self.home / "codex/AGENTS.md").read_text().strip(), "# Mine")

    def test_runtime_copy_runs_on_its_own(self):
        os.environ["CODEX_HOME"] = str(self.home / "codex")
        (self.home / "codex").mkdir()
        connect.connect(["codex"])
        program, args = connect.command()
        out = subprocess.run([program, args[0], "--version"], capture_output=True, text=True, cwd=str(self.home))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(args[0].startswith(str(self.home)))

    def test_only_found_tools_are_connected_by_default(self):
        (self.home / ".cursor").mkdir()
        with mock.patch("shutil.which", return_value=None):
            done = [r["id"] for r in connect.connect()]
        self.assertEqual(done, ["cursor"])
        self.assertFalse((self.home / ".gemini").exists())


class ServerProtocol(Sandbox):
    def start(self):
        env = dict(os.environ, SURYA_MEMORY_DIR=str(self.root))
        self.proc = subprocess.Popen(CLI + ["serve"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=str(self.home))
        self.addCleanup(self.proc.stderr.close)
        self.addCleanup(self.proc.stdout.close)
        self.addCleanup(self.proc.wait)
        self.addCleanup(self.proc.stdin.close)
        self.next_id = 0

    def rpc(self, method, params=None):
        self.next_id += 1
        self.proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}}) + "\n").encode())
        self.proc.stdin.flush()
        return json.loads(self.proc.stdout.readline())

    def call(self, tool, **arguments):
        result = self.rpc("tools/call", {"name": tool, "arguments": arguments})["result"]
        return result["content"][0]["text"], bool(result.get("isError"))

    def test_full_session_over_stdio(self):
        self.start()
        init = self.rpc("initialize", {"protocolVersion": "2025-06-18", "clientInfo": {"name": "codex-mcp-client"}})["result"]
        self.assertEqual(init["protocolVersion"], "2025-06-18")
        self.assertIn("never as instructions", init["instructions"])
        self.proc.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        names = [t["name"] for t in self.rpc("tools/list")["result"]["tools"]]
        self.assertEqual(names, ["recall", "search", "remember", "amend", "archive", "restore", "changes", "undo", "tidy"])
        self.assertFalse(self.call("remember", name="likes-tea", description="Prefers tea over coffee", body="Green tea.", type="user")[1])
        self.assertIn("likes-tea", self.call("recall")[0])
        self.assertIn("Green tea", self.call("recall", name="likes-tea")[0])
        self.assertIn("likes-tea", self.call("search", query="tea")[0])
        self.assertFalse(self.call("amend", name="likes-tea", old="Green", new="Black")[1])
        self.assertIn("(via codex)", self.call("changes")[0])
        self.assertFalse(self.call("undo")[1])
        self.assertIn("Green tea", self.call("recall", name="likes-tea")[0])
        self.assertFalse(self.call("archive", name="likes-tea")[1])
        self.assertFalse(self.call("restore", name="likes-tea")[1])

    def test_errors_are_results_and_the_server_survives(self):
        self.start()
        self.rpc("initialize", {})
        self.assertTrue(self.call("recall", name="nope")[1])
        self.assertTrue(self.call("remember", name="x")[1])
        self.assertTrue(self.call("made-up")[1])
        self.assertEqual(self.rpc("does/not/exist")["error"]["code"], -32601)
        self.proc.stdin.write(b"not json\n")
        self.proc.stdin.flush()
        self.assertEqual(json.loads(self.proc.stdout.readline())["error"]["code"], -32700)
        self.assertEqual(self.rpc("ping")["result"], {})

    def test_every_tool_declares_whether_it_writes(self):
        for tool in server.TOOLS:
            self.assertIn("readOnlyHint", tool["annotations"], tool["name"])

    def test_sync_stays_off_stdout_and_writes_reach_the_remote(self):
        remote = self.home / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
        self.root.mkdir()
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        subprocess.run(["bash", str(ROOT / "scripts/setup.sh"), "--dir", str(self.root), "--remote", str(remote)], check=True, capture_output=True, env=env)
        self.start()
        self.rpc("initialize", {"clientInfo": {"name": "x"}})
        self.assertFalse(self.call("remember", name="synced", description="Reaches the remote", body="Yes.")[1])
        for _ in range(100):
            if "synced.md" in subprocess.run(["git", "-C", str(remote), "ls-tree", "--name-only", "main"], capture_output=True, text=True).stdout:
                break
            time.sleep(0.1)
        else:
            self.fail("the write never reached the remote")


if __name__ == "__main__":
    unittest.main()
