# OpenClaw reference pack — read order

Use these files **in order** when engineering prompts or generating `.openclaw/` plugins. Autocoder attaches this whole pack to broadcast runs when **OpenClaw reference bootstrap** is enabled (default: on).

| # | File | Purpose |
|---|------|---------|
| 0 | `00_INDEX_AND_ORDER.md` | This manifest |
| 1 | `01_MACHINE_HOST_MAP.md` | **This PC** — roots, env vars, outputs, CDP |
| 2 | `02_OPENCLAW_PLUGIN_CONTRACT.md` | Plugin shape, TypeScript rules, failure modes |
| 3 | `03_DESKTOP_APPS_PLAYBOOK.md` | Chrome, Claude, Cursor, VS Code, OpenCode, shells, local tools |
| 4 | `04_AUTOCODER_INTEGRATION.md` | How Autocoder feeds context and saves artifacts |
| 5 | `MASTER_TASK_ONE_SHOT.md` | Paste-ready **master task** for the broadcast box |
| 6 | `OPENCLAW_GITHUB_AND_RUN.md` | Clone from GitHub, `PYTHONPATH`, acceptance defaults, outputs |

**Non-negotiables for generated code**

- Valid Node imports: `import * as os from 'node:os'` or `import { homedir } from 'node:os'` — never `import { os } from 'os'`.
- Never add npm dependency on built-in `crypto`.
- Strip delimiter junk (`JSON===`, `TypeScript` glued to `import`). Each file must be valid source on its own.
