# immersio

Claude Code plugin: short answers in the terminal, and a one-screen HTML infographic only when you ask for it with `/viz`. Infographics appear live on a local page.

## Use

```sh
claude --plugin-dir /path/to/immersio      # or install it from a marketplace
```

- `/output-style immersio:viz-concise`: answers lead with the result; a plain question gets 1-3 sentences, full detail on request. Coding behavior is unchanged (`keep-coding-instructions: true`). Not forced on (`force-for-plugin: false`).
- `/viz`: the last answer as an infographic. `/viz <topic>`: a topic. `/viz diff`: the current `git diff`.
- Open the URL from the `/viz` reply (default `http://localhost:4321`). New infographics appear at the top without a reload.

Requirements: Python 3.8+ (stdlib only) and a POSIX `sh` (Git Bash on Windows, which Claude Code uses for hooks). Without Python the session starts normally and shows a one-line notice.

## How it works

```
SessionStart ─ hooks/run.sh ─ server/hook.py start ─┬─ server alive? register session in .viz/server.json
                                                    └─ else spawn server/server.py (detached) and wait for its port
SessionEnd   ─ hooks/run.sh ─ server/hook.py end   ─── remove session; the server stops itself when none is left
server.py watchdog (every 1 s): stops when no registered Claude Code process is alive (covers kill -9)
```

- One server per project, shared by all its sessions. State lives in `<project>/.viz/` (`server.json`, `server.log`, `NNN-slug.html`), which carries its own `.gitignore`.
- Port 4321, or a free one when it's taken. Override with `IMMERSIO_PORT`.
- Endpoints: `/` shell, `/style.css` shared style, `/files` list (JSON), `/events` SSE (scans `.viz/` every 0.5 s), `/f/<name>` one infographic, `/health`.

### Security

- Listens on 127.0.0.1 only and rejects foreign `Host` headers (DNS rebinding).
- `/f/<name>` accepts only a plain `*.html` name inside `.viz/` (anything else is 404) and sends a CSP that blocks every external resource and network call, plus `sandbox allow-scripts`.
- The shell embeds each infographic in `<iframe sandbox="allow-scripts">` without `allow-same-origin`.

