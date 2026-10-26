"""Acceptance tests for the viz server lifecycle and HTTP contract (stdlib only).

    python -m unittest discover -s tests -v

Each test gets its own temp project and preferred port. A sleeping Python process stands in for
Claude Code (IMMERSIO_CLAUDE_PID), so "kill -9 on Claude Code" is a kill of that process.
"""
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))
import registry  # noqa: E402

HOOK = os.path.join(ROOT, "server", "hook.py")
RUN_SH = os.path.join(ROOT, "hooks", "run.sh")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_until(condition, timeout, step=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(step)
    return condition()


def get(port, path, host=None):
    """GET with the path sent verbatim: http.client doesn't normalize "../" like browsers and curl do."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path, headers={"Host": host} if host else {})
        resp = conn.getresponse()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
    finally:
        conn.close()


class Project:
    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="immersio-test-")
        self.port = free_port()
        self.paths = registry.Paths(self.dir)
        self._claudes = []

    def fake_claude(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        self._claudes.append(proc)
        return proc

    def hook(self, command, claude, **payload):
        env = dict(os.environ, CLAUDE_PROJECT_DIR=self.dir, IMMERSIO_CLAUDE_PID=str(claude.pid),
                   IMMERSIO_PORT=str(self.port))
        result = subprocess.run([sys.executable, HOOK, command], input=json.dumps(payload).encode(),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=20)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout) if result.stdout.strip() else None

    def start(self, claude, session_id, source="startup"):
        return self.hook("start", claude, session_id=session_id, source=source)

    def end(self, claude, session_id, reason="prompt_input_exit"):
        return self.hook("end", claude, session_id=session_id, reason=reason)

    def state(self):
        return registry.read_state(self.paths)

    def write(self, name, title="Test", body="<p>hi</p>"):
        path = os.path.join(self.paths.viz, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write("<!doctype html><html><head><title>%s</title></head><body>%s</body></html>" % (title, body))
        return path

    def cleanup(self):
        state = self.state() or {}
        for proc in self._claudes:
            proc.kill()
            proc.wait()
        if state.get("pid"):
            wait_until(lambda: not registry.pid_alive(state["pid"]), 5)
        shutil.rmtree(self.dir, ignore_errors=True)


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        self.project = Project()
        self.addCleanup(self.project.cleanup)

    def test_start_under_1s_and_second_session_shares_server(self):
        p = self.project
        claude_a, claude_b = p.fake_claude(), p.fake_claude()
        t0 = time.monotonic()
        out = p.start(claude_a, "a")
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 1.0)
        state = p.state()
        self.assertEqual(out["hookSpecificOutput"]["additionalContext"],
                         "viz: %s, graphics only after /viz" % state["url"])
        self.assertEqual(get(state["port"], "/")[0], 200)

        p.start(claude_b, "b")
        state_b = p.state()
        self.assertEqual((state_b["pid"], state_b["port"]), (state["pid"], state["port"]))
        self.assertEqual([s["id"] for s in state_b["sessions"]], ["a", "b"])

    def test_start_is_idempotent(self):
        p = self.project
        claude = p.fake_claude()
        p.start(claude, "a")
        first = p.state()
        p.start(claude, "a", source="compact")
        second = p.state()
        self.assertEqual(second["pid"], first["pid"])
        self.assertEqual(second["sessions"], [{"id": "a", "pid": claude.pid}])

    def test_last_session_end_stops_server(self):
        p = self.project
        claude_a, claude_b = p.fake_claude(), p.fake_claude()
        p.start(claude_a, "a")
        p.start(claude_b, "b")
        pid = p.state()["pid"]

        p.end(claude_a, "a")
        time.sleep(1.5)  # longer than a watchdog period
        self.assertTrue(registry.pid_alive(pid))

        p.end(claude_b, "b")
        self.assertTrue(wait_until(lambda: not registry.pid_alive(pid), 3))
        self.assertIsNone(p.state())

    def test_server_stops_when_claude_is_killed(self):
        p = self.project
        claude = p.fake_claude()
        p.start(claude, "a")
        pid = p.state()["pid"]
        claude.kill()  # SIGKILL / TerminateProcess: no SessionEnd hook runs
        claude.wait()
        self.assertTrue(wait_until(lambda: not registry.pid_alive(pid), 3))
        self.assertIsNone(p.state())

    def test_clear_hands_over_session_without_restart(self):
        p = self.project
        claude = p.fake_claude()
        p.start(claude, "old")
        pid = p.state()["pid"]
        p.end(claude, "old", reason="clear")
        p.start(claude, "new", source="clear")
        state = p.state()
        self.assertEqual(state["pid"], pid)
        self.assertEqual(state["sessions"], [{"id": "new", "pid": claude.pid}])
        p.end(claude, "new")
        self.assertTrue(wait_until(lambda: not registry.pid_alive(pid), 3))

    def test_busy_port_falls_back_to_free_one(self):
        p = self.project
        with socket.socket() as blocker:
            blocker.bind(("127.0.0.1", p.port))
            blocker.listen()
            p.start(p.fake_claude(), "a")
            state = p.state()
            self.assertNotEqual(state["port"], p.port)
            self.assertEqual(get(state["port"], "/")[0], 200)

    def test_crashed_server_is_replaced(self):
        p = self.project
        claude = p.fake_claude()
        p.start(claude, "a")
        old = p.state()["pid"]
        subprocess.run(["taskkill", "/F", "/PID", str(old)] if os.name == "nt" else ["kill", "-9", str(old)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        wait_until(lambda: not registry.pid_alive(old), 3)
        p.start(claude, "a", source="compact")
        new = p.state()
        self.assertNotEqual(new["pid"], old)
        self.assertEqual(get(new["port"], "/health")[0], 200)

    @unittest.skipUnless(shutil.which("sh"), "needs sh")
    def test_launcher_without_python_reports_and_does_not_block(self):
        result = subprocess.run([shutil.which("sh"), RUN_SH, "start"], input=b"{}", stdout=subprocess.PIPE,
                                env=dict(os.environ, PATH=""), timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertIn("Python 3.8+ not found", json.loads(result.stdout)["systemMessage"])


class HttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.project = Project()
        cls.project.start(cls.project.fake_claude(), "http")
        cls.port = cls.project.state()["port"]

    @classmethod
    def tearDownClass(cls):
        cls.project.cleanup()

    def test_paths_outside_viz_are_404(self):
        self.project.write("001-ok.html")
        for path in ["/f/../../etc/passwd", "/f/..%2F..%2Fetc%2Fpasswd", "/f/%2e%2e/server.json",
                     "/f/server.json", "/f/.gitignore", "/f/..", "/f/", "/f/sub/001-ok.html",
                     "/f/../server/registry.py", "/nope"]:
            self.assertEqual(get(self.port, path)[0], 404, path)

    def test_infographic_is_served_locked_down(self):
        self.project.write("002-locked.html", body="<p>locked</p>")
        status, headers, body = get(self.port, "/f/002-locked.html?v=1")
        self.assertEqual(status, 200)
        csp = headers["content-security-policy"]
        self.assertIn("default-src 'none'", csp)
        self.assertIn("sandbox allow-scripts", csp)
        self.assertIn("http://127.0.0.1:%d" % self.port, csp)
        self.assertIn(b'immersio:"height"', body)
        self.assertLess(body.index(b"<p>locked</p>"), body.index(b"immersio"))

    def test_shell_and_style(self):
        status, headers, body = get(self.port, "/")
        self.assertEqual(status, 200)
        self.assertEqual(re.findall(rb'setAttribute\("sandbox", "([^"]*)"\)', body), [b"allow-scripts"])
        status, headers, _ = get(self.port, "/style.css")
        self.assertEqual((status, headers["content-type"]), (200, "text/css; charset=utf-8"))

    def test_foreign_host_is_rejected(self):
        self.assertEqual(get(self.port, "/files", host="evil.example:%d" % self.port)[0], 403)
        self.assertEqual(get(self.port, "/files", host="localhost:%d" % self.port)[0], 200)

    def test_files_lists_newest_first_with_titles(self):
        self.project.write("009-older.html", title="Older")
        self.project.write("010-newer.html", title="Newer &amp; better")
        self.project.write("notes.txt")
        self.assertTrue(wait_until(lambda: "010-newer.html" in get(self.port, "/files")[2].decode(), 2))
        items = json.loads(get(self.port, "/files")[2])
        names = [i["name"] for i in items]
        self.assertLess(names.index("010-newer.html"), names.index("009-older.html"))
        self.assertNotIn("notes.txt", names)
        self.assertEqual(items[names.index("010-newer.html")]["title"], "Newer & better")

    def test_sse_pushes_new_file_and_resyncs_after_reconnect(self):
        first = SSE(self.port)
        before = first.next_files(3)
        self.project.write("020-live.html", title="Live")
        t0 = time.monotonic()
        while "020-live.html" not in [i["name"] for i in first.next_files(3)]:
            pass
        self.assertLess(time.monotonic() - t0, 1.5)
        self.assertNotIn("020-live.html", [i["name"] for i in before])
        first.close()

        again = SSE(self.port)  # a reloaded tab gets the current list right away
        self.assertIn("020-live.html", [i["name"] for i in again.next_files(3)])
        again.close()


class SSE:
    def __init__(self, port):
        self.conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        self.conn.request("GET", "/events")
        self.sock = self.conn.sock  # getresponse() hands the socket over to the response
        self.resp = self.conn.getresponse()
        assert self.resp.getheader("Content-Type").startswith("text/event-stream")

    def next_files(self, timeout):
        """Data of the next `files` event."""
        self.sock.settimeout(timeout)
        event, data = None, None
        while True:
            line = self.resp.fp.readline().decode("utf-8").rstrip("\r\n")
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data = line[5:].strip()
            elif line == "" and event == "files" and data is not None:
                return json.loads(data)

    def close(self):
        self.resp.close()
        self.sock.close()


if __name__ == "__main__":
    unittest.main()
