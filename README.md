# immersio

A Claude Code plugin that keeps terminal answers short and turns an answer into a one-screen visual when you ask for it.

Long explanations are hard to read in a terminal, and a diagram is often the faster way to see structure. immersio splits the two jobs: the terminal stays terse, and `/viz` draws the structure of an answer as an HTML infographic that appears live on a local page next to the terminal. Nothing is drawn unless you run `/viz`.

## Quick start

```sh
claude --plugin-dir /path/to/immersio      # or install it from a marketplace
```

1. Optional: `/output-style immersio:viz-concise` for short, result-first answers.
2. Ask anything, then run `/viz`.
3. Open the URL from the reply (default `http://localhost:4321`). New infographics appear at the top without a reload.

Requirements: Python 3.8+ (stdlib only) and a POSIX `sh` (Git Bash on Windows, which Claude Code uses for hooks). Without Python the session starts normally and shows a one-line notice.

## What you get

### `/viz`

| Command | Draws |
|---|---|
| `/viz` | the main point of the previous answer |
| `/viz <topic>` | that topic, grounded in the conversation and the code |
| `/viz diff` | the current `git diff`: files touched, shape of the change, risks |

Each run writes one new file, `.viz/NNN-slug.html`. One infographic carries one idea and fits about one screen. It uses the shared `/style.css` (light and dark themes, color variables) and nothing external.

### Visual alphabet

The form follows the content, not habit. `/viz` picks from a fixed set of forms using the DSRP lens (Distinctions, Systems, Relationships, Perspectives; Cabrera): what is it and what is it not, what are its parts, what acts on what, who looks from where.

| Form | Use for |
|---|---|
| Thesis | definition, conclusion, assumption |
| Flow | process, sequence, causality |
| Relation map | dependencies, impact, part-whole |
| Table / matrix | comparison, trade-offs, alternatives |
| Hierarchy | decomposition into parts |
| Mind map | gather thoughts |
| Concept map | organize understanding, with labeled links |
| System map | parts, boundary, flows, feedback loops |
| Mental model | what someone believes about how it works |
| Conceptual model | a new way to think about a complex system |
| Source | code, file, doc excerpt, input data |

Other rules: several forms can be combined, trade-offs are shown from at least two perspectives, epistemic status (fact, hypothesis, risk) is tagged, and provenance stays subtle. The full contract is in [`skills/viz/SKILL.md`](skills/viz/SKILL.md).

### Output style `viz-concise`

The first sentence is the result. A plain question gets 1-3 sentences; details come on request. Errors, failing test output and security warnings stay complete. Coding behavior is unchanged (`keep-coding-instructions: true`). It is not forced on (`force-for-plugin: false`) and never mentions or offers infographics.

## Layout

```
.claude-plugin/plugin.json    plugin manifest
skills/viz/SKILL.md           the /viz contract: subject, form, file name, HTML rules
output-styles/viz-concise.md  the concise answer style
hooks/                        SessionStart / SessionEnd entry (run.sh)
server/                       hook.py, registry.py, server.py, index.html, style.css
tests/                        acceptance tests
.viz/                         generated per project: server.json, server.log, NNN-slug.html
```

## How it works

```
SessionStart ─ hooks/run.sh ─ server/hook.py start ─┬─ server alive? register session in .viz/server.json
                                                    └─ else spawn server/server.py (detached) and wait for its port
SessionEnd   ─ hooks/run.sh ─ server/hook.py end   ─── remove session; the server stops itself when none is left
server.py watchdog (every 1 s): stops when no registered Claude Code process is alive (covers kill -9)
```

- One server per project, shared by all its sessions. State lives in `<project>/.viz/`, which carries its own `.gitignore`.
- Port 4321, or a free one when it's taken. Override with `IMMERSIO_PORT`.
- Endpoints: `/` shell, `/style.css` shared style, `/files` list (JSON), `/events` SSE (scans `.viz/` every 0.5 s), `/f/<name>` one infographic, `/health`.

### Security

- Listens on 127.0.0.1 only and rejects foreign `Host` headers (DNS rebinding).
- `/f/<name>` accepts only a plain `*.html` name inside `.viz/` (anything else is 404) and sends a CSP that blocks every external resource and network call, plus `sandbox allow-scripts`.
- The shell embeds each infographic in `<iframe sandbox="allow-scripts">` without `allow-same-origin`.
