# Desktop apps playbook — automation & OpenClaw tool design

Design **one plugin or tool group per major surface**. Prefer **CDP** for Chrome, **CLI / IPC** for editors where stable, and **Windows UI automation** only when no API exists.

## 1. Google Chrome (Claude web, Gemini, ChatGPT, etc.)

| Goal | Approach |
|------|------------|
| Attach / drive | Chrome with **remote debugging**; connect over CDP (Autocoder already does this per corner). |
| New tab / URL | CDP `Page.navigate` or `Target.createTarget`. |
| Input / submit | CDP Input domain + DOM snapshots; avoid coordinates when possible. |
| Multi-profile | Separate user data dirs + ports (mirror Autocoder `CDP_CORNER_PORTS`). |

**OpenClaw tools:** `chrome_list_targets`, `chrome_attach`, `chrome_evaluate` (wrap CDP with strict URL allowlists).

## 2. Claude (browser)

Treat as **a web app inside Chrome**. Selectors drift — tools should accept **CSS selector overrides** from config, not hardcode only.

## 3. Cursor

| Goal | Approach |
|------|------------|
| Open folder | `cursor.exe "<path>"` (if on PATH) or full path under Local AppData. |
| CLI | `cursor` CLI when installed (`cursor --help`). |
| Deep integration | **Workspace MCP / rules** live in `.cursor/` — generate files there from OpenClaw, don’t fight the UI. |

Typical install hint: `%LOCALAPPDATA%\Programs\cursor\Cursor.exe` (verify on machine).

## 4. Visual Studio Code

| Goal | Approach |
|------|------------|
| CLI | `code .` / `code --folder-uri`. |
| Settings | JSON under `%APPDATA%\Code\User\`. |
| Extensions | `code --install-extension id` (document, don’t run blindly). |

## 5. OpenCode

Provider chain in this repo resolves e.g. `%LOCALAPPDATA%\OpenCode\opencode-cli.exe`. OpenClaw tools should **read `OPENCODE_EXE` env** with that fallback.

## 6. CMD (`cmd.exe`)

- **Start in directory:** `cmd.exe /c "cd /d C:\path && script.cmd"`.
- Prefer **non-interactive** flags; set `PYTHONUTF8=1` for Python on Windows when needed.

## 7. PowerShell (`pwsh` / `powershell`)

- **Execution policy:** document `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once; tools should not bypass security silently.
- Use `-NoProfile -NonInteractive -Command "..."` for automation.

## 8. AutoEmerald

**User-defined** desktop application — not in standard PATH assumptions.

- Add `machine.json` key `autoemerald_exe`.
- OpenClaw tool: `autoemerald_launch` (optional args), `autoemerald_status` (window title check via optional helper).

## 9. GBR

**Undefined acronym** — do not invent behavior.

- Ship a **stub plugin** `gbr-bridge` with tools: `gbr_set_path`, `gbr_launch`, `gbr_version` that read `gbr_exe` from `%USERPROFILE%\.openclaw\machine.json` until the user fills real semantics (game build runner, custom broker, etc.).

## Cross-cutting patterns

- **Idempotency:** “ensure window” tools should no-op if already correct.
- **Timeouts:** every subprocess and CDP wait needs a cap.
- **Logging:** structured JSON lines to stderr for OpenClaw host to aggregate.
