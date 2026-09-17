# Autocoder

AI-powered browser automation tool that sends coding tasks to Gemini (or any AI chat) via Chrome DevTools Protocol, then endlessly improves the generated code through iterative feedback loops.

## Features

- **CDP Browser Automation** — Direct DOM manipulation via WebSocket, no blind clicking
- **Endless Improvement Loop** — Sends task, extracts code, feeds it back with rotating improvement focuses
- **8 Selectable Improvement Focuses** — Deep Code Dive, Extra Features, Pressure Test, Explore & Expand, Beautiful GUI, Solid & Functional, Reference Images, Review & Grade
- **Perfection Loop** — Cycles all selected focuses, auto-stops when code stops improving
- **Smart Recovery** — Detects when AI returns chat instead of code, resets conversation and asks AI to self-diagnose
- **File Attachments** — Attach .py, .json, .c, .md, or any code file as context for every iteration
- **Stagnation Detection** — MD5 hash comparison catches when code stops evolving
- **Expansion Mode** — When code plateaus, switches to generating new companion modules
- **Model Cycling** — Randomly rotates providers/models (Gemini/OpenRouter/Claude/ChatGPT/Copilot) on a user-set interval
- **Auto-Save** — Every iteration saves to Downloads with clean naming
- **State Persistence** — Stop and resume without losing progress
- **Windows Startup Resume** — Optional auto-start + endless worker resume on reboot/sign-in
- **OpenClaw reference pack** — On startup, Autocoder can auto-attach `docs/openclaw_reference_pack/*.md`, switch the profile to **OpenClaw / sovereign stack**, turn on **Perfection Loop**, **Expand on stagnation**, **Outside-the-box**, and seed the task from `MASTER_TASK_ONE_SHOT.md` when the task box is still empty. To disable: in `~/.autocoder/coding_profile_settings.json` set `"openclaw_reference_bootstrap": false`. That preset also turns **strict acceptance off** and clears **acceptance commands** so OpenClaw-style TS drops are not blocked by `npm test` with no project.

## OpenClaw (GitHub clone and run)

1. Clone/pull this repo from GitHub.
2. On Windows, set `PYTHONPATH` to include **this repo’s parent** (so `python -m Autocoder` resolves) and optionally `claude_interaction_tool` beside it — see `docs/openclaw_reference_pack/OPENCLAW_GITHUB_AND_RUN.md`.
3. Run `python -m Autocoder` from that parent directory, connect a browser session, use profile **OpenClaw / sovereign stack**, then **Start Autocoding**.
4. Full checklist: `docs/openclaw_reference_pack/OPENCLAW_GITHUB_AND_RUN.md`.

## Quick Start

```bash
# Install dependencies
pip install customtkinter pyautogui pyperclip websocket-client

# Run
python -m gemini_coder_web
```

1. Click **Launch CDP Browser** — opens a dedicated Chrome instance
2. Log into Gemini (or any AI chat) in the browser
3. Click **Grab** on a session card to connect
4. Type a task, select improvement focuses, click **Start Autocoding**
5. Optional: enable **Rotate models/providers in Chrome** and set the interval
6. Optional: enable Windows startup toggles so work resumes after restart

## Tutorials / Recommended Methods

- **Interactive broadcast:** use `Start Autocoding` / `Stop All` in the app UI.
- **Backend/Frontend split:** use the build-mode buttons (`Backend only`, `Frontend only`, `Full (both)`).
- **Endless worker:** launch build-mode and let `run_endless.py` keep iterating with recovery/restart logic.
- **Resume after reboot:** enable startup toggles in the UI; Autocoder relaunches and can auto-start the endless worker.

## How It Works

```
Task → Engineered Prompt → Gemini builds code → Extract code from response
  ↓                                                        ↓
  ← ← ← ← ← Feed code back with next improvement focus ← ←
```

Each iteration:
1. Takes the current codebase
2. Applies the next improvement focus (e.g., "Pressure Test")
3. Sends the full codebase + directive to Gemini
4. Extracts the improved code from the response
5. Saves to Downloads, repeats with next focus

## Requirements

- Python 3.11+
- Chrome/Chromium browser
- A Gemini (or other AI) account

## Build Executable

```bash
python build_autocoder.py
```

Creates a standalone `.exe` in `dist/Autocoder/`.
