"""Immersio viz server: the shell page, the shared style and the .viz/*.html infographics.

Stdlib only, Python >= 3.8. Listens on 127.0.0.1 only. Started by hook.py; stops by itself
when no registered Claude Code session is alive (watchdog), so a killed session leaves nothing behind.

    python server.py --root <project> [--port 4321] [--standalone]
"""
import argparse
import html
import json
import os
import re
import socket
import socketserver
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import registry

HERE = os.path.dirname(os.path.abspath(__file__))
SCAN_INTERVAL = 0.5  # seconds between .viz/ scans
WATCHDOG_TICKS = 2  # scans between session liveness checks
HEARTBEAT = 15  # seconds between SSE keep-alive comments
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,150}\.html")
TITLE_RE = re.compile(rb"<title[^>]*>(.*?)</title>", re.I | re.S)
NUMBER_RE = re.compile(r"\d+")

SHELL_CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; frame-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'"
)
# Infographics: nothing leaves the page. The origin is spelled out because a sandboxed document
# has an opaque origin, which 'self' may not match in every browser.
INFOGRAPHIC_CSP = (
    "default-src 'none'; style-src 'unsafe-inline' {origin}; script-src 'unsafe-inline'; "
    "img-src data: blob:; font-src data:; media-src data: blob:; base-uri 'none'; form-action 'none'; "
    "sandbox allow-scripts"
)
# Appended to every infographic: reports its height so the shell can size the iframe without scrollbars.
SIZER = (
    b'<script>(function(){var last=0;function post(){'
    b'var h=Math.ceil(document.documentElement.getBoundingClientRect().height);'
    b'if(h!==last){last=h;parent.postMessage({immersio:"height",h:h},"*");}}'
    b'if(window.ResizeObserver){new ResizeObserver(post).observe(document.documentElement);}'
    b'addEventListener("load",post);post();})();</script>'
)


class Gallery:
    """The current list of infographics. SSE streams wait on it for changes."""

    def __init__(self, viz_dir):
        self.viz_dir = viz_dir
        self.version = 0
        self.items = []
        self._changed = threading.Condition()
        self._key = None
        self._titles = {}  # (name, mtime_ns) -> title

    def scan(self):
        found = []
        try:
            with os.scandir(self.viz_dir) as entries:
                for entry in entries:
                    if NAME_RE.fullmatch(entry.name) and entry.is_file():
                        st = entry.stat()
                        found.append((entry.name, st.st_mtime_ns, st.st_size))
        except OSError:
            pass
        found.sort()
        if found == self._key:
            return
        current = {(n, m) for n, m, _ in found}
        self._titles = {k: v for k, v in self._titles.items() if k in current}
        items = [{"name": n, "title": self._title(n, m), "mtime": m // 1000000} for n, m, _ in found]
        items.sort(key=_order, reverse=True)
        with self._changed:
            self._key = found
            self.items = items
            self.version += 1
            self._changed.notify_all()

    def wait(self, seen_version, timeout):
        """Returns (version, items) once the version differs from `seen_version` or the timeout passes."""
        with self._changed:
            if self.version == seen_version:
                self._changed.wait(timeout)
            return self.version, self.items

    def _title(self, name, mtime_ns):
        key = (name, mtime_ns)
        if key not in self._titles:
            title = ""
            try:
                with open(os.path.join(self.viz_dir, name), "rb") as f:
                    match = TITLE_RE.search(f.read(16384))
                if match:
                    title = " ".join(html.unescape(match.group(1).decode("utf-8", "replace")).split())
            except OSError:
                pass
            self._titles[key] = title[:120] or name[:-5]
        return self._titles[key]


def _order(item):
    """Newest last: by the NNN prefix, then by modification time."""
    match = NUMBER_RE.match(item["name"])
    return (int(match.group()) if match else 0, item["mtime"])


class Handler(BaseHTTPRequestHandler):
    server_version = "immersio"
    sys_version = ""

    def do_GET(self):
        # Reject foreign Host headers: blocks DNS rebinding, i.e. a web page reading this server
        # through a domain that resolves to 127.0.0.1.
        host = (self.headers.get("Host") or "").lower()
        if host not in self.server.allowed_hosts:
            return self._send(403, b"forbidden host\n", "text/plain; charset=utf-8")
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._static("index.html", "text/html; charset=utf-8", {"Content-Security-Policy": SHELL_CSP})
        elif path == "/style.css":
            self._static("style.css", "text/css; charset=utf-8")
        elif path == "/files":
            self._json(self.server.gallery.items)
        elif path == "/events":
            self._events()
        elif path == "/health":
            self._json({"app": "immersio", "pid": os.getpid()})
        elif path.startswith("/f/"):
            self._infographic(path[3:], "http://" + host)
        else:
            self._not_found()

    def _infographic(self, name, origin):
        # The name is used verbatim (no URL-decoding) and must be a plain *.html file name, so
        # "/f/../../etc/passwd" and "/f/..%2Fserver.json" never resolve. realpath() also stops symlinks.
        viz = self.server.paths.viz
        if not NAME_RE.fullmatch(name):
            return self._not_found()
        full = os.path.realpath(os.path.join(viz, name))
        if os.path.normcase(os.path.dirname(full)) != os.path.normcase(os.path.realpath(viz)):
            return self._not_found()
        try:
            with open(full, "rb") as f:
                body = f.read()
        except OSError:
            return self._not_found()
        end = body.lower().rfind(b"</body>")
        body = body[:end] + SIZER + body[end:] if end >= 0 else body + SIZER
        csp = INFOGRAPHIC_CSP.format(origin=origin)
        self._send(200, body, "text/html; charset=utf-8", {"Content-Security-Policy": csp})

    def _events(self):
        """Server-Sent Events: the full file list on connect and after every change."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        version = None
        try:
            self.wfile.write(b"retry: 1000\n\n")
            while True:
                new_version, items = self.server.gallery.wait(version, HEARTBEAT)
                if new_version != version:
                    version = new_version
                    self.wfile.write(b"event: files\ndata: " + json.dumps(items).encode("utf-8") + b"\n\n")
                else:
                    self.wfile.write(b": ping\n\n")  # detects closed tabs, keeps proxies from timing out
        except OSError:
            pass  # the tab went away

    def _static(self, name, content_type, headers=None):
        with open(os.path.join(HERE, name), "rb") as f:
            self._send(200, f.read(), content_type, headers)

    def _json(self, value):
        self._send(200, json.dumps(value).encode("utf-8"), "application/json")

    def _not_found(self):
        self._send(404, b"not found\n", "text/plain; charset=utf-8")

    def _send(self, status, body, content_type, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # SSE reconnects and iframe loads would flood the log


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second socket bind a port that is already in use,
    # which would hide a busy port; exclusive binding makes the fallback below work.
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        # Skips HTTPServer.server_bind, whose reverse DNS lookup can take seconds on Windows.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def bind(port):
    try:
        return Server(("127.0.0.1", port), Handler)
    except OSError:
        return Server(("127.0.0.1", 0), Handler)  # preferred port busy: the OS picks a free one


def announce(paths, port, url):
    """Writes pid/port/url into the registry. Runs while the spawning hook holds the lock and waits for it."""
    state = registry.read_state(paths) or {}
    state.update(pid=os.getpid(), port=port, url=url, started=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    registry.write_state(paths, state)


def should_stop(paths, httpd):
    """Watchdog check. When it returns True the socket is closed and the registry entry removed."""
    lock = registry.Lock(paths.lock, 0)
    if not lock.acquire():
        return False  # a hook is updating the registry; look again next tick
    try:
        state = registry.read_state(paths)
        if not state or state.get("pid") != os.getpid():
            return True  # registry deleted or taken over by another server: not ours to serve
        sessions = state.get("sessions", [])
        live = registry.live_sessions(sessions)
        if live:
            if len(live) != len(sessions):
                state["sessions"] = live
                registry.write_state(paths, state)
            return False
        # Close the port before releasing the lock, so a session starting right now
        # can bind the same port for its new server.
        httpd.shutdown()
        httpd.server_close()
        registry.remove_state(paths)
        return True
    finally:
        lock.release()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Immersio viz server")
    parser.add_argument("--root", required=True, help="project directory; infographics live in <root>/.viz")
    parser.add_argument("--port", type=int, default=registry.DEFAULT_PORT)
    parser.add_argument("--standalone", action="store_true", help="ignore the session registry (manual runs)")
    args = parser.parse_args(argv)

    paths = registry.Paths(args.root)
    paths.ensure()
    httpd = bind(args.port)
    port = httpd.server_address[1]
    url = "http://localhost:%d" % port
    httpd.paths = paths
    httpd.allowed_hosts = {"localhost:%d" % port, "127.0.0.1:%d" % port}
    httpd.gallery = Gallery(paths.viz)
    httpd.gallery.scan()
    if not args.standalone:
        announce(paths, port, url)
    print("immersio: serving %s at %s" % (paths.viz, url), flush=True)

    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
    try:
        tick = 0
        while True:
            time.sleep(SCAN_INTERVAL)
            httpd.gallery.scan()
            tick += 1
            if not args.standalone and tick % WATCHDOG_TICKS == 0 and should_stop(paths, httpd):
                break
    except KeyboardInterrupt:
        pass
    print("immersio: stopped", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
