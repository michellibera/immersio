---
name: viz
description: Render the last answer, a topic, or the git diff as a one-screen HTML infographic shown live on the Immersio page.
argument-hint: "[topic | diff]"
disable-model-invocation: true
allowed-tools: Read Glob Edit(./.viz/**) Bash(git diff) Bash(git diff *)
---

# /viz: one infographic, one idea

Request: `$ARGUMENTS`

## 1. Subject

- Empty: the main point of your previous answer in this conversation. If there is none yet, say so in one sentence and stop.
- `diff`: run `git diff HEAD --stat` and `git diff HEAD` (plain `git diff` when the repo has no commits). Show what changed and why it matters: files touched, the shape of the change, risks. No changes: say so in one sentence and stop, creating no file.
- Anything else: that topic, grounded in this conversation and the code.

## 2. Form

The form follows the content, not habit: pick from this alphabet by the kind of information, never default to prose. Combine several forms when the content has several kinds.

| Form | Content | Build with |
|---|---|---|
| Thesis | definition, conclusion, assumption, short idea | `h1` + `.callout` |
| Flow | process, sequence, causality, data flow | `.flow` (`.vertical` for long ones) |
| Relation map | dependencies, impact, part-whole, systems | inline SVG: nodes, typed edge labels |
| Table / matrix | comparison, criteria, trade-offs, alternatives | `.table` |
| Hierarchy | decomposition into parts | nested `ul`, or `.grid` of `.card`s |
| Source | code, file, doc excerpt, input data | `pre` + `.muted` path (`file:line`) |

- No `.card` frame when the content reads fine without one; use it to group parallel items, not to wrap everything.
- Show only the relations the one idea needs; leave the rest out.
- Mark epistemic status where it matters: observed fact, decision, conclusion, hypothesis. Use `.tag` (`.ok` fact, `.warn` hypothesis, `.bad` risk) and keep contradictions visible but quiet.
- Provenance stays subtle: a `.muted` source line under the claim, not a banner.
- Changing the form never changes the meaning: the same idea as a table instead of a flow is a new file with the same claims.

## 3. File name

In parallel: Read `${CLAUDE_PROJECT_DIR}/.viz/server.json` (its `url`) and Glob `.viz/*.html` in `${CLAUDE_PROJECT_DIR}`.

Write to `${CLAUDE_PROJECT_DIR}/.viz/NNN-slug.html`: NNN is the highest existing number + 1, zero-padded to 3 digits (the first is `001`); slug is 2-5 lowercase ASCII words joined with `-`. Never open or edit earlier infographics: each one is a fresh, complete file.

## 4. HTML contract

- Complete document: `<!doctype html>`, `<html lang="…">`, `<meta charset="utf-8">`, viewport meta, a short `<title>` (it becomes the card title), `<link rel="stylesheet" href="/style.css">`.
- Nothing external: no CDN, libraries, web fonts, remote images or network calls. The server's CSP blocks them, so they would just not render. Inline `<style>` and `<script>` are fine.
- Diagrams from flexbox, grid and inline SVG.
- One main idea that fits about one screen (~1000×700 px). All text in the language of this conversation.
- Light and dark themes come from `/style.css` via `prefers-color-scheme`. Give every custom color (CSS or SVG `style="fill:…"`) as a variable, never a literal: `--bg --surface --fg --muted --line --accent --ok --warn --bad`, each color also with a `-soft` tint (e.g. `--accent-soft`).
- No `vh`/`vw` heights: the page sizes the frame to the content.

Classes in `/style.css`:

| Class | Use |
|---|---|
| `.card` | Bordered box; put several in a `.grid` |
| `.flow` | Row of `.node` and `.arrow`; `.flow.vertical` stacks top to bottom |
| `.node` | Box in a flow; `<small>` for a sub-label; variants `.accent` `.ok` `.warn` `.bad` |
| `.arrow` | Connector between nodes; its text is the edge label |
| `.table` | Data table; `td.num` right-aligns numbers |
| `.callout` | The key takeaway; variants `.ok` `.warn` `.bad` |
| `.tag` | Small pill label; same variants |
| `.muted` | Secondary text |

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Auth flow</title>
<link rel="stylesheet" href="/style.css">
</head>
<body>
<h1>Auth flow</h1>
<p class="muted">One sentence of context.</p>
<div class="flow">
  <div class="node">Browser</div>
  <div class="arrow">POST /login</div>
  <div class="node accent">API<small>issues JWT</small></div>
  <div class="arrow">verify</div>
  <div class="node">DB</div>
</div>
<div class="callout">The one thing to remember.</div>
</body>
</html>
```

## 5. Reply

Exactly one sentence in the conversation's language: `Done: #N <title> — <url>`, e.g. `Done: #3 Auth flow — http://localhost:4321` (in Polish: `Gotowe: #3 Przepływ autoryzacji — http://localhost:4321`). If `server.json` is missing, still write the file and add that the viz server isn't running, so the session needs a restart.
