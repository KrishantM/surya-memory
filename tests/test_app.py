"""Tests for the app's local API: who may call it, and what the chat is allowed to do.

Runs the real server on a throwaway port against a throwaway HOME.
Run: python3 -m unittest discover -s tests -v
"""

import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from surya_memory import app  # noqa: E402
from surya_memory.store import Store  # noqa: E402


class Harness(unittest.TestCase):
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
        Store(self.root, project="alpha", host="mac").remember("first-note", "A note that exists", "Hello.", source="test")
        self.memory = app.Memory(self.root)
        handler = type("BoundHandler", (app.Handler,), {"memory": self.memory})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        self.port = self.memory.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def call(self, method, path, body=None, token=True, headers=None, raw=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        self.addCleanup(connection.close)
        sent = {"Host": f"127.0.0.1:{self.port}"}
        if token:
            sent["X-Token"] = self.memory.token if token is True else token
        data = raw
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            sent["Content-Type"] = "application/json"
        sent.update(headers or {})
        connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for key, value in sent.items():
            connection.putheader(key, value)
        if data is not None and "Content-Length" not in sent:
            connection.putheader("Content-Length", str(len(data)))
        connection.endheaders()
        if data is not None:
            connection.send(data)
        response = connection.getresponse()
        return response.status, response.read(), response


class Api(Harness):
    def test_api_needs_the_token(self):
        self.assertEqual(self.call("GET", "/api/notes", token=False)[0], 403)
        self.assertEqual(self.call("GET", "/api/notes", token="wrong")[0], 403)
        self.assertEqual(self.call("GET", "/api/notes")[0], 200)

    def test_token_in_the_url_does_not_open_the_api(self):
        self.assertEqual(self.call("GET", f"/api/notes?t={self.memory.token}", token=False)[0], 403)

    def test_post_needs_the_token(self):
        self.assertEqual(self.call("POST", "/api/archive", {"name": "first-note"}, token=False)[0], 403)
        self.assertTrue(Store(self.root).get("first-note"))

    def test_other_host_is_refused_even_with_the_token(self):
        # A rebinding attack reaches this port under the attacker's own hostname.
        self.assertEqual(self.call("GET", "/", headers={"Host": f"evil.example:{self.port}"})[0], 403)
        self.assertEqual(self.call("GET", "/api/notes", headers={"Host": f"evil.example:{self.port}"})[0], 403)

    def test_other_origin_is_refused_even_with_the_token(self):
        status = self.call("POST", "/api/archive", {"name": "first-note"}, headers={"Origin": "https://evil.example"})[0]
        self.assertEqual(status, 403)
        self.assertTrue(Store(self.root).get("first-note"))

    def test_own_origin_is_fine(self):
        self.assertEqual(self.call("POST", "/api/reviewed", {}, headers={"Origin": f"http://127.0.0.1:{self.port}"})[0], 200)

    def test_only_json_is_accepted(self):
        # A plain cross-site form can only send text/plain or form encoding.
        self.assertEqual(self.call("POST", "/api/reviewed", raw=b"{}", headers={"Content-Type": "text/plain"})[0], 415)

    def test_bad_bodies(self):
        self.assertEqual(self.call("POST", "/api/reviewed", raw=b"{nope", headers={"Content-Type": "application/json"})[0], 400)
        self.assertEqual(self.call("POST", "/api/reviewed", raw=b"", headers={"Content-Type": "application/json", "Content-Length": "abc"})[0], 400)
        self.assertEqual(self.call("POST", "/api/reviewed", raw=b"", headers={"Content-Type": "application/json", "Content-Length": "-5"})[0], 400)
        self.assertEqual(self.call("POST", "/api/reviewed", raw=b"", headers={"Content-Type": "application/json", "Content-Length": str(app.MAX_BODY + 1)})[0], 413)

    def test_page_is_locked_down(self):
        status, html, response = self.call("GET", "/", token=False)
        policy = response.getheader("Content-Security-Policy")
        self.assertEqual(status, 200)
        self.assertIn(f"script-src 'nonce-{self.memory.nonce}'", policy)
        self.assertIn("default-src 'none'", policy)
        self.assertIn("frame-ancestors 'none'", policy)
        self.assertNotIn(b"__NONCE__", html)
        self.assertNotIn(self.memory.token.encode(), html)

    def test_saving_and_refusing_a_secret(self):
        self.assertEqual(self.call("POST", "/api/remember", {"name": "new-note", "description": "A new note", "body": "Text."})[0], 200)
        self.assertTrue(Store(self.root).get("new-note"))
        status = self.call("POST", "/api/remember", {"name": "leak", "description": "Leak", "body": "ghp_" + "a1B2" * 9})[0]
        self.assertEqual(status, 400)
        self.assertIsNone(Store(self.root).get("leak"))

    def test_unknown_route(self):
        self.assertEqual(self.call("GET", "/api/nope")[0], 400)
        self.assertEqual(self.call("GET", "/etc/passwd")[0], 403)

    def test_chat_refuses_an_unknown_engine(self):
        status, body, _ = self.call("POST", "/api/chat", {"message": "hi", "engine": "bash"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body.decode().splitlines()[0])["type"], "error")


class Idle(Harness):
    def test_server_stops_once_the_page_has_gone(self):
        self.memory.last_seen -= app.IDLE_LIMIT + 1
        with mock.patch.object(app.time, "sleep"), mock.patch.object(self.httpd, "shutdown") as shutdown:
            app._close_when_idle(self.httpd, self.memory)
        shutdown.assert_called_once()

    def test_any_call_counts_as_the_page_being_open(self):
        self.memory.last_seen -= 100
        self.call("GET", "/api/engines")
        self.assertLess(app.time.monotonic() - self.memory.last_seen, 50)


class FirstRun(Harness):
    def test_new_machine_asks_for_setup(self):
        memory = app.Memory(None)
        self.assertTrue(memory.needs_setup())
        self.assertEqual(memory.state(), {"needs_setup": True, "version": app.__version__})

    def test_pinned_store_skips_setup(self):
        (self.home / ".surya-memory").mkdir()
        (self.home / ".surya-memory" / "store-path").write_text(str(self.root))
        self.assertFalse(app.Memory(None).needs_setup())


class Chat(Harness):
    def command(self, engine):
        with tempfile.TemporaryDirectory() as folder:
            return self.memory._command(engine, "hello", folder)[0]

    def test_claude_gets_memory_tools_and_nothing_else(self):
        command = self.command("claude")
        self.assertEqual(command[command.index("--tools") + 1], "")
        self.assertEqual(command[command.index("--allowedTools") + 1], "mcp__surya-memory__*")
        self.assertIn("--strict-mcp-config", command)
        self.assertIn("disableAllHooks", command[command.index("--settings") + 1])

    def test_codex_cannot_touch_the_machine(self):
        command = self.command("codex")
        self.assertEqual(command[command.index("-s") + 1], "read-only")
        self.assertIn("--ephemeral", command)

    def test_parsing_claude(self):
        tool = json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "mcp__surya-memory__amend"}]}})
        self.assertEqual(app.Memory._parse("claude", tool), [{"type": "activity", "text": "Updating a memory"}])
        done = json.dumps({"type": "result", "result": "Done."})
        self.assertEqual(app.Memory._parse("claude", done), [{"type": "reply", "text": "Done."}])
        failed = json.dumps({"type": "result", "is_error": True})
        self.assertEqual(app.Memory._parse("claude", failed)[0]["type"], "error")

    def test_parsing_codex(self):
        tool = json.dumps({"type": "item.started", "item": {"type": "mcp_tool_call", "tool": "recall"}})
        self.assertEqual(app.Memory._parse("codex", tool), [{"type": "activity", "text": "Reading memory"}])
        said = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "Yes."}})
        self.assertEqual(app.Memory._parse("codex", said), [{"type": "reply", "text": "Yes."}])

    def test_junk_lines_are_ignored(self):
        self.assertEqual(app.Memory._parse("claude", "not json"), [])


if __name__ == "__main__":
    unittest.main()
