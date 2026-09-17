# Machine host map (this workstation)

**Host identity:** Windows 10/11, primary user profile and “AI tooling” trees live under the paths below. When generating launchers, shell profiles, or OpenClaw tools that spawn processes, **prefer these literals** unless the user overrides.

## Canonical roots

| Symbol | Path |
|--------|------|
| `USERPROFILE` | `C:\Users\default.LAPTOP-S2O9G7EP` |
| Desktop AI workspace | `C:\Users\default.LAPTOP-S2O9G7EP\Desktop\AI2` |
| Autocoder repo | `C:\Users\default.LAPTOP-S2O9G7EP\Desktop\AI2\CLaud tool\Autocoder` |
| Claude interaction / gemini stack | `C:\Users\default.LAPTOP-S2O9G7EP\Desktop\AI2\CLaud tool\claude_interaction_tool` |
| Autocoder_src | `C:\Users\default.LAPTOP-S2O9G7EP\Desktop\AI2\Autocoder_src` |
| User Autocoder state | `%USERPROFILE%\.autocoder` → `C:\Users\default.LAPTOP-S2O9G7EP\.autocoder` |
| Downloads (saved runs) | `%USERPROFILE%\Downloads` |
| Autocoder scratch/final outputs | `%USERPROFILE%\Downloads\autocoder_outputs\scratch` and `\candidate_final` |

## Python / Autocoder launch

From parent of package (e.g. `...\CLaud tool`):

```text
set PYTHONPATH=%USERPROFILE%\Desktop\AI2\CLaud tool\claude_interaction_tool;%USERPROFILE%\Desktop\AI2\CLaud tool
python -m Autocoder
```

Packaged runs may use `pythonw -m Autocoder` from Startup.

## OpenClaw on disk (convention)

Assume plugins live under:

```text
%USERPROFILE%\.openclaw\<plugin-id>\
```

State, undo, and reports:

```text
%USERPROFILE%\.openclaw\state.json   (if global — adjust per your design)
%USERPROFILE%\.openclaw\undo\...
%USERPROFILE%\.openclaw\test-reports\...
```

## CDP / Chrome (Autocoder)

- Default debug ports per corner are owned by Autocoder’s `cdp_client` / session cards.
- When building **browser automation tools**, respect that **only the attached Chrome profile** is fair game; do not assume Linux paths.

## Placeholders you must fill in playbooks

- **AutoEmerald** — set `AUTOEMERALD_EXE` to the real `.exe` path when known.
- **GBR** — set `GBR_EXE` (or document the acronym) to the real launcher; until then, expose a **config-only** tool that reads paths from `%USERPROFILE%\.openclaw\machine.json`.
