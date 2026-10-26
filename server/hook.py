"""Claude Code SessionStart/SessionEnd hook: one viz server per project, shared by its sessions.

Usage (through hooks/run.sh): hook.py start|end, with the hook's JSON input on stdin.
A failure here never blocks the session: start reports it as a systemMessage, end on stderr.
"""
import http.client
import json
import os
import subprocess
import sys
import time

import registry

HERE = os.path.dirname(os.path.abspath(__file__))
START_TIMEOUT = 5.0  # seconds to wait for the registry lock and for a new server to report its port
END_LOCK_TIMEOUT = 1.0  # SessionEnd hooks share a 1.5 s budget
# /clear and /resume end a session and start its successor in the same Claude Code process.
SUCCESSOR_EVENTS = ("clear", "resume")


def main(argv):
    command = argv[1] if len(argv) > 1 else ""
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
    except ValueError:
        payload = {}
    project = os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd()
    paths = registry.Paths(project)
    try:
        if command == "start":
            url = start(paths, payload)
            emit({"hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": "viz: %s, graphics only after /viz" % url,
            }})
        elif command == "end":
            end(paths, payload)
        else:
            print("usage: hook.py start|end", file=sys.stderr)
            return 1
    except Exception as e:
        if command == "start":
            emit({"systemMessage": "immersio: viz server not started: %s" % e})
        else:
            print("immersio: %s" % e, file=sys.stderr)
    return 0


def emit(output):
    sys.stdout.write(json.dumps(output) + "\n")


def start(paths, payload):
    """Registers the session; starts the server if none is alive. Idempotent. Returns the URL."""
    session_id = payload.get("session_id") or "unknown"
    pid = registry.claude_pid()
    paths.ensure()
    with registry.Lock(paths.lock, START_TIMEOUT):
        state = registry.read_state(paths) or {}
        sessions = registry.live_sessions(state.get("sessions", []))
        if payload.get("source") in SUCCESSOR_EVENTS and pid is not None:
            sessions = [s for s in sessions if s.get("pid") != pid]
        sessions = [s for s in sessions if s.get("id") != session_id]
        sessions.append({"id": session_id, "pid": pid})
        if server_responds(state):
            state["sessions"] = sessions
            registry.write_state(paths, state)
        else:
            state = spawn_server(paths, sessions)
    return state["url"]


def end(paths, payload):
    """Unregisters the session. The server notices an empty list within a second and stops itself."""
    if payload.get("reason") in SUCCESSOR_EVENTS:
        return  # the successor's SessionStart replaces this entry; stopping now would only restart the server
    if not os.path.exists(paths.state):
        return
    with registry.Lock(paths.lock, END_LOCK_TIMEOUT):
        state = registry.read_state(paths)
        if not state:
            return
        session_id = payload.get("session_id")
        state["sessions"] = [
            s for s in registry.live_sessions(state.get("sessions", [])) if s.get("id") != session_id
        ]
        registry.write_state(paths, state)


def server_responds(state):
    """True when the registered pid is alive AND answers as this server (guards against pid reuse)."""
    pid, port = state.get("pid"), state.get("port")
    if not pid or not port or not registry.pid_alive(pid):
        return False
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=0.5)  # http.client ignores proxy env vars
    try:
        conn.request("GET", "/health")
        resp = conn.getresponse()
        info = json.loads(resp.read().decode("utf-8"))
        return resp.status == 200 and info.get("app") == "immersio" and info.get("pid") == pid
    except (OSError, ValueError):
        return False
    finally:
        conn.close()


def spawn_server(paths, sessions):
    """Starts a detached server and waits for it to write its pid and port into the registry.

    Runs under the registry lock; the server's single startup write happens while we wait here.
    """
    registry.write_state(paths, {"sessions": sessions})
    port = os.environ.get("IMMERSIO_PORT") or str(registry.DEFAULT_PORT)
    cmd = [sys.executable, os.path.join(HERE, "server.py"), "--root", paths.project, "--port", port]
    with open(paths.log, "w", encoding="utf-8") as log:
        child = popen_detached(cmd, log, paths.project)
    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        state = registry.read_state(paths) or {}
        if state.get("pid") == child.pid and state.get("url"):
            return state
        if child.poll() is not None:
            break
        time.sleep(0.02)
    raise RuntimeError("server did not come up, see " + paths.log)


def popen_detached(cmd, log, cwd):
    """Survives the hook and the terminal. stdout/stderr go to the log, never to the hook's pipes,
    otherwise Claude Code would wait for the server to exit before finishing the hook."""
    kwargs = dict(stdin=subprocess.DEVNULL, stdout=log, stderr=log, cwd=cwd, close_fds=True)
    if os.name != "nt":
        return subprocess.Popen(cmd, start_new_session=True, **kwargs)
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        # Leave Claude Code's job object, if any, so the server outlives the session that started it.
        return subprocess.Popen(cmd, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **kwargs)
    except OSError:  # the job forbids breakaway
        return subprocess.Popen(cmd, creationflags=flags, **kwargs)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
