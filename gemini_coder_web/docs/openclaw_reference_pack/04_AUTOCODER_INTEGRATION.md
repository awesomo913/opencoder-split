# Autocoder integration (this repo)

## What Autocoder does in broadcast mode

1. Engineers your **task** (if Prompt Architect / `prompt_engine` is available) with build target, strategy flags, and **attached reference files** (this pack).
2. Sends prompts to the **configured browser session** (Gemini, OpenRouter-in-Chrome, etc.).
3. Loops **improvement focuses** — optionally **Perfection Loop** (cycle all selected focuses until stop), **Expand on stagnation**, and **Outside-the-box** on a timer.
4. Saves outputs under `Downloads\autocoder_outputs\` and may append **MASTER** logs under `~\.autocoder\projects\...`.

## Flags you should assume ON for “sovereign stack” runs

- **Perfection Loop**
- **Expand on stagnation**
- **Outside-the-box** (with sane frequency, e.g. 10+ minutes)
- **Explore & Expand** among selected focuses
- **Build adjacent to reference** when this pack is attached

## Acceptance gates

The **OpenClaw / sovereign stack** profile sets **strict acceptance off** and **empty commands** so browser-generated `.openclaw/` TypeScript is not rejected by `npm test` when no Node project exists. See `OPENCLAW_GITHUB_AND_RUN.md` to re-enable a gate once you have a real repo with `package.json` / `tsconfig.json`.

## Restart / resume

- State: `~\.autocoder\broadcast_state.json`
- After reboot, user must **Launch CDP / Grab** session again; Autocoder can auto-resume the loop when sessions exist.

## PYTHONPATH (dev)

When running from source, parent folder must include both `Autocoder` package root and `claude_interaction_tool` on `PYTHONPATH` (see `01_MACHINE_HOST_MAP.md`).
