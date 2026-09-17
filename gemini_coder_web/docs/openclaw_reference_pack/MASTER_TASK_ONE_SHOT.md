# MASTER TASK — OpenClaw sovereign stack (paste into Autocode box)

**Objective:** Build and iterate a **coherent OpenClaw plugin set** that automates **this Windows workstation** (paths in attached `01_MACHINE_HOST_MAP.md`), with first-class support for **Chrome (CDP)**, **Claude in browser**, **Cursor**, **VS Code**, **OpenCode**, **CMD**, **PowerShell**, plus **stub bridges** for **AutoEmerald** and **GBR** until real binaries are configured in `%USERPROFILE%\.openclaw\machine.json`.

**Rules:**

1. Read **all attached reference `.md` files** in order before writing code.
2. Emit plugins as **separate** `--- FILE: relative/path ---` blocks with **valid** TypeScript/JSON per `02_OPENCLAW_PLUGIN_CONTRACT.md` (no bad `os` imports, no `crypto` npm dep).
3. Phase 1: `machine-bootstrap` plugin — writes/updates `machine.json` schema (exe paths, Chrome debug ports optional, workspace roots).
4. Phase 2: `shell-runner` — allowlisted `cmd` / `pwsh` execution with cwd + timeout + capture stdout/stderr as JSON.
5. Phase 3: `editor-launcher` — Cursor + VS Code open-folder tools using paths from `machine.json` + standard fallbacks from `03_DESKTOP_APPS_PLAYBOOK.md`.
6. Phase 4: `chrome-bridge` — document CDP attachment pattern; if full CDP is too large, ship **clear** stubs with TODO hooks matching Autocoder’s multi-port model.
7. Phase 5: `opencode-bridge` — wraps `OPENCODE_EXE` resolution.
8. Phase 6: `autoemerald-bridge` + `gbr-bridge` — config + launch stubs only.
9. Every mutating tool creates an **undo record** under `%USERPROFILE%\.openclaw\undo\<plugin-id>\`.
10. After each phase, output **runnable** `package.json` scripts (`typecheck` using `tsc --noEmit`) where applicable.

**Stop condition:** User clicks Stop — until then, use **Perfection Loop** behavior: cycle deep review, security, documentation, and integration passes across the **whole** plugin set, not only the last file touched.
