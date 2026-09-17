# OpenClaw plugin contract (tools & hooks)

## Layout

```text
.openclaw/<plugin-id>/
  openclaw.plugin.json   # id, name, version, description, entry
  package.json           # name, openclaw.extensions, devDependencies
  index.ts               # default export: { tools: [...], hooks?: ... }
  tests/                 # optional
```

## `openclaw.plugin.json`

- `id`: kebab-case, stable (used in paths and config).
- `entry`: typically `index.ts`.
- Match `package.json` `name` to `openclaw-<id>` or similar for clarity.

## Tool object shape

Each tool:

- `name`: unique string, snake_case preferred.
- `description`: what it does + when **not** to call it.
- `inputSchema`: JSON Schema object (`type`, `properties`, `required`).
- `handler`: async function `(args) => JSON-serializable result`.

Return **objects**, not bare strings, for anything non-trivial (easier logging and undo).

## TypeScript correctness (hard rules)

1. **`os` module:** use `import * as os from 'node:os'` or named imports `import { homedir, tmpdir } from 'node:os'`. **Invalid:** `import { os } from 'os'`.
2. **`crypto`:** use `import { createHash, randomUUID } from 'node:crypto'`. **Do not** add `"crypto"` to `package.json` dependencies.
3. **`fs`:** prefer `node:fs` / `node:fs/promises` for clarity.
4. **Atomic writes:** write temp file in same directory, then `rename` over target.
5. **Concurrency:** JSON state + multi-writer = risk; document single-writer assumption or add file lock / SQLite for scale.

## Undo / reversibility

- Before mutating: snapshot minimal prior state (or full file) to `%USERPROFILE%\.openclaw\undo\<plugin-id>\`.
- Undo tool should **validate path** is under undo root (path jail).

## Security

- Any tool that runs shell commands must: allowlist commands, validate cwd, never pass unsanitized user text to `exec`.
- “Discord / remote → local command” bridges are **high risk** — gate behind explicit env flag + confirmation tool.

## Output hygiene

Never emit:

- `JSON===` prefixes
- `TypeScript` token concatenated onto `import`
- Multi-file bundles without clear `--- FILE: path ---` separators **and** valid per-file bodies

If the host splits on `--- FILE:`, each segment must parse as the claimed language.
