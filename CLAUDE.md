# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Hermes Web UI is a browser-based interface for the [Hermes Agent](https://hermes-agent.nousresearch.com/). It provides full parity with the CLI experience — three-panel layout (sidebar sessions, center chat, right workspace browser) — using Python server + vanilla JS frontend. No build step, no bundler.

## Commands

```bash
# Run the server (auto-discovers Hermes agent, Python, state dir)
./start.sh

# Or manually
HERMES_WEBUI_PORT=8787 venv/bin/python server.py

# Health check
curl http://127.0.0.1:8787/health

# Run all tests
pytest tests/ -v --timeout=60

# Run a single test file
venv/bin/python -m pytest tests/test_sprint1.py -v

# Run a single test
venv/bin/python -m pytest tests/test_sprint1.py::test_health -v

# Tail server logs
tail -f /tmp/webui-mvp.log
```

## Architecture

### Backend (Python)

```
server.py              ~81 lines: HTTP handler dispatch + auth middleware
api/
  routes.py            All GET + POST route handlers
  streaming.py         SSE engine, _run_agent_streaming(), cancel support
  models.py            Session model + CRUD
  config.py            Discovery, globals, model detection
  profiles.py          Profile state management
  auth.py              Optional password authentication
  helpers.py           HTTP helpers (j(), bad(), require(), safe_resolve())
  workspace.py         File ops: list_dir, read_file_content
  upload.py           Multipart parser for file uploads
```

### Frontend (Vanilla JS, served from static/)

```
index.html             HTML template
style.css             All CSS (dark theme, mobile responsive)
ui.js                 DOM helpers, renderMd, tool cards, model dropdown
workspace.js          File tree, preview, file operations
sessions.js           Session CRUD, list rendering, search
messages.js           send(), SSE handlers, approval
panels.js             Cron, skills, memory, profiles, settings
commands.js           Slash command registry + autocomplete
boot.js               Event wiring, mobile nav, voice input
```

### Key Data Flows

**Chat round-trip**: `send()` → POST /api/chat/start → daemon thread runs AIAgent → SSE events stream to browser via /api/chat/stream

**SSE event types**: `token`, `tool`, `approval`, `done`, `error`

**State directory** (`~/.hermes/webui-mvp/`):
- `sessions/*.json` — one JSON per session
- `settings.json` — user preferences
- `workspaces.json` — registered workspace paths
- `projects.json` — session groups

### Runtime Environment

- Server uses Hermes agent's Python venv: `<agent-dir>/venv/bin/python`
- Agent modules imported via `sys.path.insert(0, parent_dir)`
- `HERMES_HOME` defaults to `~/.hermes`

Environment variables: `HERMES_WEBUI_PORT`, `HERMES_WEBUI_PASSWORD`, `HERMES_WEBUI_STATE_DIR`, `HERMES_WEBUI_DEFAULT_WORKSPACE`, `HERMES_CONFIG_PATH`

## Critical Rules (Regressions)

These patterns have been broken and fixed multiple times — do not re-introduce:

1. **deleteSession() must NEVER call newSession()** — deleting does not create. If active session deleted and others exist, load the most recent. If none, show empty state.

2. **/api/upload check must appear BEFORE read_body() in do_POST** — `read_body()` consumes the HTTP body. Upload parsing needs the raw body. Order matters.

3. **run_conversation() takes task_id=, NOT session_id=** — wrong keyword raises TypeError silently.

4. **on_token callback: guard `if text is None: return`** — None is the end-of-stream sentinel.

5. **send() must capture activeSid BEFORE any await** — session can change during awaits.

6. **All SESSIONS dict accesses must hold LOCK** — `with LOCK: ...`

7. **do NOT expose tracebacks to API clients** — 500 responses return `{"error": "Internal server error"}`

## Adding Endpoints

GET endpoint pattern (in routes.py):
```python
if parsed.path == '/api/your/endpoint':
    param = qs.get('param', [''])[0]
    if not param:
        return j(self, {'error': 'param is required'}, status=400)
    return j(self, {'result': value})
```

POST endpoint pattern:
```python
if parsed.path == '/api/your/endpoint':
    body = read_body(self)
    value = body.get('field', '')
    if not value:
        return bad(self, 'field is required')
    return j(self, {'ok': True, 'data': result})
```

## Test Isolation

Tests run on port 8788 with isolated state directory (`~/.hermes/webui-mvp-test`). Production data never touched. The test state dir is wiped before each test session.
