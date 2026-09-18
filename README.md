# OpenCoder Split

> A self-driving "endless improvement" coding agent — two different ways to run it, because the desktop app and the browser-automation build were never merged into one.

OpenCoder Split takes a coding task and iterates on it repeatedly, keeping a history of every pass. It ships as two parallel implementations: `gemini_coder/`, a CustomTkinter desktop app that talks to the Gemini API directly, and `gemini_coder_web/`, a browser-automation build that drives an AI chat UI over the Chrome DevTools Protocol instead of using an API key, with its own provider chain and fleet manager for routing across multiple backends.

## Features
- Desktop coding-agent GUI (`gemini_coder/`) that calls the Gemini API directly, with task queue + history + expansion/compaction of context
- Browser-automation variant (`gemini_coder_web/`) that drives AI chat UIs via Chrome DevTools Protocol — no API key needed
- Provider chain / fleet manager for routing tasks across multiple backends (Ollama, OpenRouter, Pi-hosted models, OpenCode)
- Auto-save and diagnostics, plus build scripts to package either build into a standalone executable
- A recorded 46-iteration example run (`examples/coding_essentials/iterations/`) showing the agent improving one file over time
- Broadcast/session-manager modules for coordinating multiple concurrent coding sessions

## Stack
Python · CustomTkinter (GUI) · Google Gemini API (`google-genai`) · Chrome DevTools Protocol via `websocket-client` · PyInstaller packaging.

## Getting started
**Requirements**
- Python 3.10+, and either a Google Gemini API key (`gemini_coder/`) or a Chrome instance with remote debugging enabled (`gemini_coder_web/`)

**Run**
```bash
# desktop app, direct Gemini API:
pip install -r gemini_coder/requirements.txt
python -m gemini_coder

# browser-automation variant:
pip install -r gemini_coder_web/requirements.txt
python -m gemini_coder_web
```

## Status
**Unmaintained / archived.** Personal project, published as-is — fork it, adapt it, take it over. No support or guarantees.

## License
[MIT](LICENSE) — free to use, fork, and build on.
