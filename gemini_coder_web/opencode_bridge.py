"""OpenCode Desktop / CLI integration — sessions + prompts over HTTP, with SQLite fallback.

OpenCode runs a local HTTP server (same API as ``opencode serve``, default port 4096).
The desktop app uses a sidecar process; pin the port so automation can connect:

  - Environment: ``OPENCODE_PORT=4096`` before launching OpenCode, or
  - Config / server picker per https://dev.opencode.ai/docs/server

Environment:

  ``OPENCODE_API_BASE`` — default ``http://127.0.0.1:4096``
  ``OPENCODE_CLI`` — full path to ``opencode-cli.exe`` if not in default install dir / PATH
  ``OPENCODE_SERVER_USERNAME`` / ``OPENCODE_SERVER_PASSWORD`` — optional HTTP basic auth

Optional offline read of session titles/paths (same DB the desktop uses on Windows):

  ``OPENCODE_DB_PATH`` — override ``%USERPROFILE%\\.local\\share\\opencode\\opencode.db``
  ``OPENCODE_SQLITE_MESSAGE_FALLBACK`` — set to ``0`` / ``false`` to skip reading assistant
  text from the local ``part`` table when the HTTP API returns empty ``parts`` (default: on).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import socket
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional, Set
from urllib.parse import quote, urljoin, urlparse

logger = logging.getLogger(__name__)

_DEFAULT_BASE = os.environ.get("OPENCODE_API_BASE", "http://127.0.0.1:4096").rstrip("/")


def opencode_message_row_text(row: dict[str, Any]) -> str:
    """Concatenate human-readable text from one GET ``/session/.../message`` row."""
    parts = row.get("parts") or []
    chunks: list[str] = []
    for p in parts:
        if not isinstance(p, dict):
            continue
        typ = p.get("type")
        if typ in ("text", "reasoning") and isinstance(p.get("text"), str):
            chunks.append(p["text"])
        elif isinstance(p.get("content"), str):
            chunks.append(p["content"])
    return "\n".join(chunks).strip()


def _sqlite_fallback_enabled() -> bool:
    v = (os.environ.get("OPENCODE_SQLITE_MESSAGE_FALLBACK") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _text_from_stored_part_obj(obj: dict[str, Any]) -> str:
    if not obj:
        return ""
    typ = obj.get("type")
    if typ in ("text", "reasoning") and isinstance(obj.get("text"), str):
        return obj["text"]
    if isinstance(obj.get("content"), str):
        return obj["content"]
    return ""


def opencode_message_text_from_sqlite(
    session_id: str,
    message_id: str,
    *,
    db_path: Optional[Path] = None,
) -> str:
    """Load assistant-visible text from ``part.data`` JSON (OpenCode 1.14+ HTTP gap).

    The desktop app streams tool/reasoning/text into SQLite while ``GET /session/:id/message``
    sometimes returns ``parts: []`` for the assistant row until much later (or indefinitely).
    """
    if not _sqlite_fallback_enabled():
        return ""
    sid = (session_id or "").strip()
    mid = (message_id or "").strip()
    if not sid or not mid:
        return ""
    path = db_path or _default_db_path()
    if not path.is_file():
        return ""
    try:
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as e:
        logger.debug("OpenCode SQLite fallback: could not open %s: %s", path, e)
        return ""
    try:
        cur = con.cursor()
        rows = cur.execute(
            "SELECT data FROM part WHERE session_id = ? AND message_id = ? ORDER BY time_updated ASC",
            (sid, mid),
        ).fetchall()
    except sqlite3.Error as e:
        logger.debug("OpenCode SQLite fallback query failed: %s", e)
        return ""
    finally:
        con.close()

    chunks: list[str] = []
    for (raw,) in rows:
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(obj, dict):
            piece = _text_from_stored_part_obj(obj)
            if piece:
                chunks.append(piece)
    return "\n".join(chunks).strip()


def newest_assistant_body_from_sqlite(
    session_id: str,
    before_message_ids: Set[str],
    *,
    db_path: Optional[Path] = None,
) -> tuple[str, Optional[list[Any]]]:
    """Return text + HTTP-shaped parts for the newest assistant not in ``before_message_ids``.

    Covers:

    * Assistant exists in HTTP with empty ``parts`` while SQLite ``part`` rows stream in.
    * SQLite updates before ``GET /session/:id/message`` shows the new assistant row.
    """
    if not _sqlite_fallback_enabled():
        return "", None
    sid = (session_id or "").strip()
    if not sid:
        return "", None
    path = db_path or _default_db_path()
    if not path.is_file():
        return "", None
    try:
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as e:
        logger.debug("OpenCode SQLite scan: open failed %s: %s", path, e)
        return "", None
    try:
        cur = con.cursor()
        mids = [
            str(r[0])
            for r in cur.execute(
                """
                SELECT id FROM message
                WHERE session_id = ? AND json_extract(data, '$.role') = 'assistant'
                ORDER BY time_updated DESC LIMIT 40
                """,
                (sid,),
            )
        ]
    except sqlite3.Error as e:
        logger.debug("OpenCode SQLite scan: query failed: %s", e)
        return "", None
    finally:
        con.close()

    for mid in mids:
        if mid in before_message_ids:
            continue
        blob = opencode_message_text_from_sqlite(sid, mid, db_path=path)
        if not blob:
            continue
        http_like_parts: list[Any] = []
        try:
            con2 = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
            cur2 = con2.cursor()
            raw_rows = cur2.execute(
                """
                SELECT data FROM part
                WHERE session_id = ? AND message_id = ?
                ORDER BY time_updated ASC
                """,
                (sid, mid),
            ).fetchall()
            con2.close()
            for (raw,) in raw_rows:
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if isinstance(obj, dict):
                    http_like_parts.append(obj)
        except sqlite3.Error:
            http_like_parts = []

        return blob, http_like_parts or None

    return "", None


def _default_db_path() -> Path:
    override = os.environ.get("OPENCODE_DB_PATH")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "share" / "opencode" / "opencode.db"


@dataclass
class OpenCodeSessionRow:
    """Row from local SQLite ``session`` table (offline / inspection)."""

    id: str
    project_id: str
    slug: str
    directory: str
    title: str
    version: str
    time_created: int
    time_updated: int
    time_archived: Optional[int]


def iter_sessions_from_sqlite(
    directory_contains: Optional[str] = None,
    *,
    db_path: Optional[Path] = None,
    include_archived: bool = False,
) -> Iterator[OpenCodeSessionRow]:
    """Yield sessions by reading ``opencode.db`` directly (no HTTP server required)."""
    path = db_path or _default_db_path()
    if not path.is_file():
        logger.warning("OpenCode DB not found at %s", path)
        return
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        cur = con.cursor()
        q = (
            "SELECT id, project_id, slug, directory, title, version, "
            "time_created, time_updated, time_archived FROM session WHERE 1=1"
        )
        params: list[Any] = []
        if directory_contains:
            like = f"%{directory_contains}%"
            q += " AND directory LIKE ?"
            params.append(like)
        if not include_archived:
            q += " AND time_archived IS NULL"
        q += " ORDER BY time_updated DESC"
        for row in cur.execute(q, params):
            yield OpenCodeSessionRow(
                id=row[0],
                project_id=row[1],
                slug=row[2],
                directory=row[3],
                title=row[4],
                version=row[5],
                time_created=row[6],
                time_updated=row[7],
                time_archived=row[8],
            )
    finally:
        con.close()


class OpenCodeHTTPError(RuntimeError):
    """Raised when OpenCode returns a non-2xx or malformed response."""

    def __init__(self, msg: str, *, status: Optional[int] = None, body: str = "") -> None:
        super().__init__(msg)
        self.status = status
        self.body = body


class OpenCodeClient:
    """Thin client for OpenCode's local OpenAPI server (see ``/doc`` on the server)."""

    def __init__(
        self,
        base_url: str = _DEFAULT_BASE,
        *,
        username: Optional[str] = None,
        password: Optional[str] = None,
        timeout_s: float = 300.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self._user = username or os.environ.get("OPENCODE_SERVER_USERNAME")
        self._password = password or os.environ.get("OPENCODE_SERVER_PASSWORD")

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Optional[dict[str, Any]] = None,
        query: Optional[dict[str, str]] = None,
        timeout_override: Optional[float] = None,
    ) -> Any:
        url = urljoin(self.base_url + "/", path.lstrip("/"))
        if query:
            qs = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in query.items())
            url = f"{url}?{qs}"
        data: Optional[bytes] = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method.upper(), headers=headers)
        if self._user and self._password is not None:
            token = base64.b64encode(
                f"{self._user}:{self._password}".encode("utf-8")
            ).decode("ascii")
            req.add_header("Authorization", f"Basic {token}")

        timeout = self.timeout_s if timeout_override is None else float(timeout_override)

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if resp.status == 204 or not raw:
                    return None
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise OpenCodeHTTPError(
                f"OpenCode HTTP {e.code}: {e.reason}",
                status=e.code,
                body=err_body,
            ) from e
        except TimeoutError as e:
            raise OpenCodeHTTPError(
                f"OpenCode request timed out after {timeout}s: {e}",
                status=None,
            ) from e
        except urllib.error.URLError as e:
            raise OpenCodeHTTPError(f"OpenCode unreachable at {self.base_url}: {e}") from e

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/global/health")

    def session_list(self) -> list[dict[str, Any]]:
        out = self._request("GET", "/session")
        if not isinstance(out, list):
            return [out] if isinstance(out, dict) else []
        return out

    def session_get(self, session_id: str) -> dict[str, Any]:
        return self._request("GET", f"/session/{quote(session_id, safe='')}")

    def session_create(self, *, title: Optional[str] = None, parent_id: Optional[str] = None) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if title is not None:
            body["title"] = title
        if parent_id is not None:
            body["parentID"] = parent_id
        return self._request("POST", "/session", body=body or {})

    def session_patch(self, session_id: str, *, title: str) -> dict[str, Any]:
        return self._request(
            "PATCH",
            f"/session/{quote(session_id, safe='')}",
            body={"title": title},
        )

    def session_delete(self, session_id: str) -> Any:
        return self._request("DELETE", f"/session/{quote(session_id, safe='')}")

    def session_fork(self, session_id: str, *, message_id: Optional[str] = None) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if message_id:
            body["messageID"] = message_id
        return self._request(
            "POST",
            f"/session/{quote(session_id, safe='')}/fork",
            body=body,
        )

    def session_messages(self, session_id: str) -> list[dict[str, Any]]:
        """GET /session/:id/message — full chat as a list of ``{info, parts}`` rows."""
        out = self._request("GET", f"/session/{quote(session_id, safe='')}/message")
        if not isinstance(out, list):
            return []
        return out

    def send_message(
        self,
        session_id: str,
        text: str,
        *,
        model: Optional[dict[str, str]] = None,
        agent: Optional[str] = None,
        no_reply: bool = False,
    ) -> Any:
        """POST /session/:id/message.

        OpenCode 1.14+ often returns an empty body (204 / no JSON) and appends the
        assistant reply to GET ``/session/:id/message``. When the POST body is empty
        and ``no_reply`` is false, we poll that endpoint until a new assistant row
        appears (same wall-clock budget as ``timeout_s``).
        """
        path = f"/session/{quote(session_id, safe='')}/message"
        initial_rows = self.session_messages(session_id)
        before_len = len(initial_rows)
        before_assistant_ids: Set[str] = set()
        for r in initial_rows:
            if (r.get("info") or {}).get("role") != "assistant":
                continue
            mid0 = (r.get("info") or {}).get("id")
            if isinstance(mid0, str):
                before_assistant_ids.add(mid0)
        t_start = time.monotonic()

        parts = [{"type": "text", "text": text}]
        body: dict[str, Any] = {"parts": parts, "noReply": no_reply}
        if model:
            body["model"] = model
        if agent:
            body["agent"] = agent

        # OpenCode often holds POST open until the model finishes (long) or returns an
        # empty body immediately. Use a short POST first so we can poll GET in parallel
        # with the same overall budget as ``timeout_s``.
        post_cap = min(30.0, max(8.0, self.timeout_s / 10.0))
        raw: Any = None
        try:
            raw = self._request("POST", path, body=body, timeout_override=post_cap)
        except OpenCodeHTTPError as exc:
            if "timed out" in str(exc).lower():
                logger.info(
                    "OpenCode POST hit %ss timeout; polling GET /session/.../message for reply",
                    post_cap,
                )
            else:
                raise

        if raw is not None:
            return raw
        if no_reply:
            return None

        poll_deadline = t_start + float(self.timeout_s)
        get_timeout = min(45.0, max(15.0, self.timeout_s / 4.0))
        getter = OpenCodeClient(
            self.base_url,
            username=self._user,
            password=self._password,
            timeout_s=get_timeout,
        )

        while time.monotonic() < poll_deadline:
            rows = getter.session_messages(session_id)
            best = ""
            best_parts: Any = None
            if len(rows) > before_len:
                for r in rows[before_len:]:
                    if (r.get("info") or {}).get("role") != "assistant":
                        continue
                    msg_id = (r.get("info") or {}).get("id")
                    blob = opencode_message_row_text(r)
                    if not blob and isinstance(msg_id, str):
                        blob = opencode_message_text_from_sqlite(session_id, msg_id)
                    if len(blob) > len(best):
                        best = blob
                        best_parts = r.get("parts")
            if not best:
                b2, p2 = newest_assistant_body_from_sqlite(session_id, before_assistant_ids)
                if b2:
                    best, best_parts = b2, p2
            if best:
                return {"text": best, "parts": best_parts}
            time.sleep(0.75)

        raise OpenCodeHTTPError(
            "Timed out waiting for assistant reply (OpenCode may still be generating; try again).",
            status=None,
        )

    def send_prompt_async(self, session_id: str, text: str, **kwargs: Any) -> None:
        """POST /session/:id/prompt_async — fire-and-forget (204)."""
        parts = [{"type": "text", "text": text}]
        body: dict[str, Any] = {"parts": parts}
        body.update({k: v for k, v in kwargs.items() if v is not None})
        self._request(
            "POST",
            f"/session/{quote(session_id, safe='')}/prompt_async",
            body=body,
        )


def host_port_from_base_url(base_url: str) -> tuple[str, int]:
    """Return host and port from an OpenCode API base URL (defaults 127.0.0.1:4096)."""
    raw = (base_url or "").strip().rstrip("/")
    if not raw:
        return "127.0.0.1", 4096
    if "://" not in raw:
        raw = f"http://{raw}"
    parsed = urlparse(raw)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 4096
    return host, port


def find_opencode_cli() -> Optional[Path]:
    """Locate ``opencode-cli`` / ``opencode`` for ``serve`` (Windows + PATH)."""
    env = (os.environ.get("OPENCODE_CLI") or "").strip()
    if env:
        p = Path(env).expanduser()
        if p.is_file():
            return p
    localappdata = os.environ.get("LOCALAPPDATA", "")
    if localappdata:
        candidate = Path(localappdata) / "OpenCode" / "opencode-cli.exe"
        if candidate.is_file():
            return candidate
    for name in ("opencode.exe", "opencode.cmd", "opencode"):
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def _tcp_port_open(host: str, port: int, timeout: float = 0.35) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def ensure_opencode_serve(
    base_url: str,
    *,
    wait_ready_s: float = 20.0,
    poll_interval_s: float = 0.35,
) -> tuple[bool, str, Optional[subprocess.Popen]]:
    """Start OpenCode ``serve`` if the API is not yet healthy.

    Returns ``(success, message, subprocess)``. The subprocess is set only when
    this call spawned it (caller may ``terminate()`` on app exit). If the server
    was already healthy, the subprocess is ``None``.
    """
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return False, "Set API base URL first (e.g. http://127.0.0.1:4096).", None
    host, port = host_port_from_base_url(base)
    probe = OpenCodeClient(base, timeout_s=3.0)
    try:
        probe.health()
        return True, "OpenCode server is already running.", None
    except OpenCodeHTTPError:
        pass

    exe = find_opencode_cli()
    if exe is None:
        return (
            False,
            "OpenCode CLI not found. Install the OpenCode desktop app or set "
            "OPENCODE_CLI to the full path of opencode-cli.exe.",
            None,
        )

    if _tcp_port_open(host, port):
        deadline = time.monotonic() + wait_ready_s
        while time.monotonic() < deadline:
            try:
                probe.health()
                return True, "OpenCode server is already running.", None
            except OpenCodeHTTPError:
                time.sleep(poll_interval_s)
        return (
            False,
            f"Port {port} is open but OpenCode did not pass the health check. "
            "Stop the other program on that port or change the API base URL.",
            None,
        )

    try:
        proc = subprocess.Popen(
            [str(exe), "serve", "--port", str(port), "--hostname", host],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        return False, f"Failed to start OpenCode: {exc}", None

    deadline = time.monotonic() + wait_ready_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return (
                False,
                f"OpenCode serve exited early (exit code {proc.returncode}).",
                proc,
            )
        try:
            probe.health()
            return True, f"Started OpenCode server at {host}:{port}.", proc
        except OpenCodeHTTPError:
            time.sleep(poll_interval_s)

    try:
        proc.terminate()
    except Exception:
        pass
    return False, f"OpenCode did not become ready within {wait_ready_s:.0f}s.", None


def _main_cli() -> None:
    import argparse

    p = argparse.ArgumentParser(description="OpenCode bridge — health, sessions, prompts")
    p.add_argument("command", choices=["health", "list-http", "list-db", "create", "rename", "fork", "send"])
    p.add_argument("--base", default=_DEFAULT_BASE, help="OpenCode API base URL")
    p.add_argument("--session", help="Session id (ses_…)")
    p.add_argument("--title", help="Title for create/rename")
    p.add_argument("--text", help="Message body for send")
    p.add_argument("--db-filter", help="Substring filter for list-db directory path")
    p.add_argument("--message-id", help="Message id for fork")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.command == "health":
        c = OpenCodeClient(args.base)
        print(json.dumps(c.health(), indent=2))
        return

    if args.command == "list-http":
        c = OpenCodeClient(args.base)
        for s in c.session_list():
            sid = s.get("id") or s.get("ID")
            title = s.get("title") or s.get("Title", "")
            print(f"{sid}\t{title}")
        return

    if args.command == "list-db":
        for row in iter_sessions_from_sqlite(directory_contains=args.db_filter):
            print(f"{row.id}\t{row.directory}\t{row.title}")
        return

    if args.command == "create":
        c = OpenCodeClient(args.base)
        print(json.dumps(c.session_create(title=args.title), indent=2))
        return

    if args.command == "rename":
        if not args.session or not args.title:
            raise SystemExit("rename needs --session and --title")
        c = OpenCodeClient(args.base)
        print(json.dumps(c.session_patch(args.session, title=args.title), indent=2))
        return

    if args.command == "fork":
        if not args.session:
            raise SystemExit("fork needs --session")
        c = OpenCodeClient(args.base)
        print(json.dumps(c.session_fork(args.session, message_id=args.message_id), indent=2))
        return

    if args.command == "send":
        if not args.session or not args.text:
            raise SystemExit("send needs --session and --text")
        c = OpenCodeClient(args.base, timeout_s=600.0)
        print(json.dumps(c.send_message(args.session, args.text), indent=2))
        return

    raise SystemExit(f"unknown command {args.command}")


if __name__ == "__main__":
    _main_cli()
