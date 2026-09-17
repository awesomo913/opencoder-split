# OpenClaw workflow — clone from GitHub and run Autocoder

## 1. Clone or update

```bash
git clone https://github.com/awesomo913/Autocoder.git
cd Autocoder
git pull
```

Use your fork’s URL if you develop on a fork instead of `awesomo913/Autocoder`.

## 2. Windows: `PYTHONPATH` (required for source runs)

Autocoder expects the **parent folder** of the `Autocoder` package on `PYTHONPATH`, plus `claude_interaction_tool` if you use that tree beside it. Example (adjust usernames/paths):

```powershell
cd "C:\Users\YOURNAME\Desktop\AI2\CLaud tool"
$env:PYTHONPATH = "C:\Users\YOURNAME\Desktop\AI2\CLaud tool\claude_interaction_tool;C:\Users\YOURNAME\Desktop\AI2\CLaud tool"
python -m Autocoder
```

Or `pythonw -m Autocoder` for a window without a console.

## 3. Open the UI and profile

1. **Launch CDP Browser** → log into Gemini (or your target) → **Grab** a session.
2. Profile **OpenClaw / sovereign stack** (use **Apply profile** if the combo does not match).
3. Confirm **Strict acceptance gate** is **off** for this preset, and **Acceptance commands** is **empty** unless you intentionally add checks (see below). This avoids false failures when the model outputs TypeScript plugin bundles that are not inside an npm project.

## 4. Reference pack

Markdown playbooks live in `docs/openclaw_reference_pack/`. With `openclaw_reference_bootstrap: true` in `%USERPROFILE%\.autocoder\coding_profile_settings.json`, Autocoder attaches them on startup and seeds the task from `MASTER_TASK_ONE_SHOT.md` when the task box is still empty or placeholder.

To disable auto-attach:

```json
"openclaw_reference_bootstrap": false
```

### Apex Frontier overdrive

In the Autocode card, enable **Apex Frontier overdrive** and pick a **lens** (plugins, automation engines, CLIs, bridges, etc.). That mode prepends a strong “reference-first R&D” mission and, when possible, embeds excerpts from a local **OpenedClaw** checkout (`HANDOFF.md`, `AGENTS.md`, `README.md`). Set `OPENEDCLAW_ROOT` to your clone path (or rely on the default `Desktop\AI2\openedclaw` if present). With `openclaw_frontier_on_bootstrap: true` in `coding_profile_settings.json`, the OpenClaw reference bootstrap also turns Frontier on for you.

## 5. Optional: add a real gate later

When you have a checkout with `package.json` / `tsconfig.json`, set **Acceptance commands** to something that matches the repo, for example:

```text
npx tsc --noEmit
```

Then turn **Strict acceptance gate** back on. The sovereign preset defaults to **no** gate so OpenClaw-style browser generations are not rejected by `npm test` with nothing to run.

## 6. Outputs

Saved files appear under `%USERPROFILE%\Downloads\autocoder_outputs\` (and related paths) per Autocoder’s auto-save rules.

## 7. OpenClaw plugins themselves

Autocoder only **generates** plugin-shaped text; it does not install OpenClaw. Copy emitted `--- FILE: .openclaw/... ---` segments into your real OpenClaw workspace, run `npm install` / `tsc` there, and register plugins in your gateway config following your OpenClaw deployment docs.
