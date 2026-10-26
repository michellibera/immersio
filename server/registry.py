"""Shared state of a project's viz server: paths, lock, registry file, process checks.

Registry file <project>/.viz/server.json:
    {"pid", "port", "url", "started", "sessions": [{"id": session_id, "pid": claude_pid}]}
Every read-modify-write of it happens under the <project>/.viz/.lock file lock.
Stdlib only, Python >= 3.8.
"""
import json
import os
import subprocess
import time

DEFAULT_PORT = 4321

# Processes that may sit between Claude Code and a hook script.
_LAUNCHERS = frozenset(
    "sh bash dash zsh ksh mksh ash fish busybox cmd powershell pwsh env nice nohup timeout py".split()
)


class Paths:
    def __init__(self, project):
        self.project = os.path.abspath(project)
        self.viz = os.path.join(self.project, ".viz")
        self.state = os.path.join(self.viz, "server.json")
        self.lock = os.path.join(self.viz, ".lock")
        self.log = os.path.join(self.viz, "server.log")

    def ensure(self):
        """Creates .viz/ and keeps it out of git without touching the project's .gitignore."""
        os.makedirs(self.viz, exist_ok=True)
        ignore = os.path.join(self.viz, ".gitignore")
        if not os.path.exists(ignore):
            with open(ignore, "w", encoding="utf-8") as f:
                f.write("# Immersio session data, never committed.\n*\n")


# --- lock -------------------------------------------------------------------

if os.name == "nt":
    import msvcrt

    def _try_lock(fd):
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd):
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(fd):
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd):
        fcntl.flock(fd, fcntl.LOCK_UN)


class Lock:
    """Exclusive inter-process file lock. The OS drops it when the holder dies, so it never goes stale."""

    def __init__(self, path, timeout):
        self.path = path
        self.timeout = timeout
        self._fd = None

    def acquire(self):
        """Returns False when the lock stayed busy for `timeout` seconds (0 = single attempt)."""
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                _try_lock(fd)
                self._fd = fd
                return True
            except OSError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    return False
                time.sleep(0.02)

    def release(self):
        if self._fd is not None:
            _unlock(self._fd)
            os.close(self._fd)
            self._fd = None

    def __enter__(self):
        if not self.acquire():
            raise TimeoutError("registry lock busy: " + self.path)
        return self

    def __exit__(self, *exc):
        self.release()


# --- registry file ----------------------------------------------------------

def read_state(paths):
    try:
        with open(paths.state, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        return None
    return state if isinstance(state, dict) else None


def write_state(paths, state):
    """Atomic replace, so readers never see a half-written file."""
    tmp = "%s.%d.tmp" % (paths.state, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    for attempt in range(50):
        try:
            os.replace(tmp, paths.state)
            return
        except PermissionError:  # Windows: another process has the file open for reading
            time.sleep(0.01 * (attempt + 1))
    os.replace(tmp, paths.state)


def remove_state(paths):
    try:
        os.remove(paths.state)
    except FileNotFoundError:
        pass


def live_sessions(sessions):
    """Drops sessions whose Claude Code process is gone. A session without a known pid is kept."""
    return [
        s for s in sessions
        if isinstance(s, dict) and (s.get("pid") is None or pid_alive(s["pid"]))
    ]


# --- processes --------------------------------------------------------------

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _k32.WaitForSingleObject.restype = wintypes.DWORD
    _k32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    _k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _k32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)

    class _ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    _k32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ProcessEntry))
    _k32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ProcessEntry))

    def pid_alive(pid):
        # Never os.kill(pid, 0) here: on Windows it calls TerminateProcess.
        if not isinstance(pid, int) or pid <= 0:
            return False
        handle = _k32.OpenProcess(0x1000 | 0x00100000, False, pid)  # QUERY_LIMITED_INFORMATION | SYNCHRONIZE
        if not handle:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: exists, owned by someone else
        try:
            return _k32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT: still running
        finally:
            _k32.CloseHandle(handle)

    def _process_lookup():
        table = {}
        snapshot = _k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
        if not snapshot or snapshot == wintypes.HANDLE(-1).value:
            return table.get
        try:
            entry = _ProcessEntry()
            entry.dwSize = ctypes.sizeof(_ProcessEntry)
            ok = _k32.Process32FirstW(snapshot, ctypes.byref(entry))
            while ok:
                table[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile)
                ok = _k32.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            _k32.CloseHandle(snapshot)
        return table.get

else:

    def pid_alive(pid):
        if not isinstance(pid, int) or pid <= 0:  # 0 and -1 would address process groups
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _proc_stat(pid):
        try:
            with open("/proc/%d/stat" % pid, encoding="utf-8", errors="replace") as f:
                data = f.read()
        except OSError:
            return None
        name = data[data.index("(") + 1:data.rindex(")")]
        ppid = int(data[data.rindex(")") + 2:].split()[1])
        return ppid, name

    def _ps(pid):
        try:
            out = subprocess.run(
                ["ps", "-o", "ppid=", "-o", "comm=", "-p", str(pid)],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True, timeout=2,
            ).stdout.split(None, 1)
        except (OSError, subprocess.SubprocessError):
            return None
        return (int(out[0]), out[1].strip()) if len(out) == 2 else None

    def _process_lookup():
        return _proc_stat if os.path.isdir("/proc/self") else _ps


def claude_pid():
    """Pid of the Claude Code process that runs this hook: the nearest ancestor that isn't a shell or Python."""
    override = os.environ.get("IMMERSIO_CLAUDE_PID")  # test seam
    if override:
        return int(override)
    lookup = _process_lookup()
    pid = os.getppid()
    for _ in range(12):
        info = lookup(pid)
        if info is None:
            return None
        ppid, name = info
        base = os.path.basename(name).lower()
        if base.endswith(".exe"):
            base = base[:-4]
        if base not in _LAUNCHERS and not base.startswith("python"):
            return pid
        pid = ppid
    return None
