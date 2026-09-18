"""Signaux locaux : est-ce que Claude ou Cursor tourne (et consomme) maintenant ?

Claude Code : `~/.claude/sessions/<pid>.json` porte un `status`
(`idle` / `busy` / `waiting` = en attente d'une action utilisateur).
Le titre IDE vient du transcript (`custom-title`, sinon `ai-title`).
Cursor local : `composerData` + bubbles (`reviewData.status=Requested` =
pending approval Skip/Run). Aussi `unfinishedRunAt`, outils loading, `shellStatus`.
Cursor cloud : listes dans state.vscdb.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

STATE_DB = Path.home() / "Library/Application Support/Cursor/User/globalStorage/state.vscdb"
CLAUDE_SESSIONS = Path.home() / ".claude" / "sessions"
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
# Fenêtre de fraîcheur d'un run Cursor (lastUpdatedAt / startedAtMs outil).
RUN_HOT_SECONDS = 15 * 60
# Un outil commencé il y a plus longtemps n'est plus « en cours ».
TOOL_HOT_SECONDS = 5 * 60
# Queue de lecture des titres Claude (les jsonl peuvent faire des Mo).
TITLE_TAIL_BYTES = 512_000
# Cache titre IDE : (mtime transcript → titre), pour sonder chaque seconde sans relire.
_title_cache: dict[str, tuple[float, str | None]] = {}
# Status Claude Code qui ne consomment pas / n'attendent rien.
CLAUDE_IDLE = frozenset({"idle", "done", "finished", "stopped", "exited", ""})
CLAUDE_WAITING = frozenset({"waiting"})
# Shell / review en attente d'approbation utilisateur (Cursor : « Pending approval »).
SHELL_PENDING = frozenset({"pending"})
SHELL_RUNNING = frozenset({"running", "loading", "pending"})
TOOL_RUNNING = frozenset({"loading"})
REVIEW_PENDING = frozenset({"requested", "pending"})
TOOL_PENDING_STATUSES = frozenset({"loading", "pending"})


@dataclass(frozen=True)
class ActivityItem:
    title: str
    since_ms: int = 0


@dataclass(frozen=True)
class Activity:
    claude_items: tuple[ActivityItem, ...] = ()
    claude_waiting: tuple[ActivityItem, ...] = ()
    cursor_items: tuple[ActivityItem, ...] = ()
    cursor_waiting: tuple[ActivityItem, ...] = ()

    @property
    def claude_sessions(self) -> int:
        return len(self.claude_items)

    @property
    def cursor_cloud(self) -> int:
        return 0

    @property
    def cursor_local(self) -> int:
        return len(self.cursor_items)

    @property
    def cursor_total(self) -> int:
        return len(self.cursor_items)

    @property
    def running_count(self) -> int:
        """Sessions / agents en cours, Claude + Cursor confondus."""
        return self.claude_sessions + self.cursor_total

    @property
    def waiting_count(self) -> int:
        """Sessions bloquées en attente d'une action utilisateur."""
        return len(self.claude_waiting) + len(self.cursor_waiting)

    def claude_rows(self) -> tuple[tuple[str, bool, int], ...]:
        """Lignes Claude : (titre, en_attente, since_ms). Blocage = ligne en plus."""
        return _activity_rows(self.claude_items, self.claude_waiting)

    def cursor_rows(self) -> tuple[tuple[str, bool, int], ...]:
        """Lignes Cursor : (titre, en_attente, since_ms). Blocage = ligne en plus."""
        return _activity_rows(self.cursor_items, self.cursor_waiting)


def _activity_rows(
    items: tuple[ActivityItem, ...],
    waiting: tuple[ActivityItem, ...],
) -> tuple[tuple[str, bool, int], ...]:
    """Toutes les actives (+), puis les blocages (−) en plus — jamais à la place."""
    active = tuple((item.title, False, item.since_ms) for item in items)
    blocked = tuple((item.title, True, item.since_ms) for item in waiting)
    return active + blocked


def _pid_alive(pid: int) -> bool:
    try:
        Path(f"/proc/{pid}").exists()  # Linux ; sur macOS on tombe dans OSError ci-dessous.
    except OSError:
        pass
    try:
        import os

        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError, PermissionError):
        return False


def _cwd_project_dir(cwd: str) -> Path | None:
    text = (cwd or "").strip()
    if not text:
        return None
    return CLAUDE_PROJECTS / text.replace("/", "-")


def _claude_ide_title(session_id: str, cwd: str) -> str | None:
    """Titre affiché dans l'IDE : dernier `custom-title`, sinon `ai-title`."""
    if not session_id:
        return None
    root = _cwd_project_dir(cwd)
    path = (root / f"{session_id}.jsonl") if root else None
    if path is None or not path.is_file():
        try:
            matches = list(CLAUDE_PROJECTS.rglob(f"{session_id}.jsonl")) if CLAUDE_PROJECTS.is_dir() else []
        except OSError:
            matches = []
        if not matches:
            return None
        path = matches[0]
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    cached = _title_cache.get(session_id)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        data = path.read_bytes()
    except OSError:
        return None
    chunk = data[-TITLE_TAIL_BYTES:] if len(data) > TITLE_TAIL_BYTES else data
    custom = ai = None
    for line in chunk.decode("utf-8", errors="ignore").splitlines():
        if "title" not in line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        if kind == "custom-title":
            title = str(event.get("customTitle") or "").strip()
            if title:
                custom = title
        elif kind == "ai-title":
            title = str(event.get("aiTitle") or "").strip()
            if title:
                ai = title
    result = custom or ai
    _title_cache[session_id] = (mtime, result)
    return result


def _claude_live() -> tuple[tuple[ActivityItem, ...], tuple[ActivityItem, ...]]:
    """Sessions Claude vivantes non idle → (toutes, celles en waiting)."""
    if not CLAUDE_SESSIONS.is_dir():
        return (), ()
    items: list[ActivityItem] = []
    waiting: list[ActivityItem] = []
    try:
        for path in sorted(CLAUDE_SESSIONS.glob("*.json")):
            if not path.name[:-5].isdigit():
                continue
            try:
                blob = json.loads(path.read_text() or "{}")
            except (OSError, json.JSONDecodeError, UnicodeError):
                continue
            if not isinstance(blob, dict):
                continue
            pid = int(blob.get("pid") or path.stem)
            if not _pid_alive(pid):
                continue
            status = str(blob.get("status") or "").strip().lower()
            if status in CLAUDE_IDLE:
                continue
            derived = str(blob.get("name") or "").strip() or path.stem
            session_id = str(blob.get("sessionId") or "").strip()
            cwd = str(blob.get("cwd") or "").strip()
            label = _claude_ide_title(session_id, cwd) or derived
            started = _as_ms(blob.get("startedAt") or 0)
            status_at = _as_ms(blob.get("statusUpdatedAt") or started)
            items.append(ActivityItem(label, status_at or started))
            if status in CLAUDE_WAITING:
                waiting.append(ActivityItem(label, status_at or started))
    except OSError:
        return (), ()
    return tuple(items), tuple(waiting)


def _agent_label(agent) -> str:
    if not isinstance(agent, dict):
        return "agent"
    for key in ("name", "title", "displayName", "summary", "id"):
        value = agent.get(key)
        if value:
            return str(value).strip()
    return "agent"


def _agent_alive(agent) -> bool:
    if not isinstance(agent, dict):
        return True
    status = str(agent.get("status") or agent.get("state") or "").lower()
    if status in ("done", "finished", "failed", "cancelled", "canceled", "archived", "complete", "completed"):
        return False
    if agent.get("isArchived") or agent.get("archived"):
        return False
    return True


def _agent_waiting(agent) -> bool:
    if not isinstance(agent, dict):
        return False
    if agent.get("hasBlockingPendingActions"):
        return True
    status = str(agent.get("status") or agent.get("state") or "").lower()
    return status in ("waiting", "needs_input", "blocked", "pending_user", "awaiting_input")


def _cursor_cloud() -> tuple[tuple[ActivityItem, ...], tuple[ActivityItem, ...]]:
    if not STATE_DB.exists():
        return (), ()
    try:
        con = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True, timeout=0.25)
    except sqlite3.Error:
        return (), ()
    try:
        rows = con.execute(
            "SELECT key, value FROM ItemTable WHERE key LIKE 'cloudAgentRepository.agents.%' "
            "OR key = 'backgroundComposer.windowBcMapping'"
        ).fetchall()
    except sqlite3.Error:
        return (), ()
    finally:
        con.close()

    items: list[ActivityItem] = []
    waiting: list[ActivityItem] = []
    for key, value in rows:
        raw = value or ""
        if key.startswith("cloudAgentRepository.agents."):
            text = raw.strip()
            if text.startswith("[") and text != "[]":
                try:
                    agents = json.loads(text)
                    if isinstance(agents, list):
                        for agent in agents:
                            if not _agent_alive(agent):
                                continue
                            label = _agent_label(agent)
                            since = _as_ms(agent.get("updatedAt") or agent.get("createdAt") or 0)
                            item = ActivityItem(label, since)
                            items.append(item)
                            if _agent_waiting(agent):
                                waiting.append(item)
                except (json.JSONDecodeError, TypeError):
                    items.append(ActivityItem("agent cloud"))
        elif key == "backgroundComposer.windowBcMapping":
            try:
                mapping = json.loads(raw)
                if isinstance(mapping, dict):
                    for ids in mapping.values():
                        if isinstance(ids, list):
                            items.extend(ActivityItem(str(i)) for i in ids)
            except (json.JSONDecodeError, TypeError):
                pass
    return tuple(items), tuple(waiting)


def _as_ms(value) -> int:
    """Epoch ms ; accepte aussi des epoch secondes ou ISO."""
    if isinstance(value, (int, float)):
        n = int(value)
        return n * 1000 if 0 < n < 10_000_000_000 else n
    if isinstance(value, str) and value:
        try:
            text = value.replace("Z", "+00:00")
            return int(datetime.fromisoformat(text).timestamp() * 1000)
        except ValueError:
            return 0
    return 0


def _cursor_composer_signals(blob: dict, now_ms: int) -> tuple[bool, bool, int, int]:
    """(en cours, en attente, since_run_ms, since_wait_ms) d'après composerData."""
    waiting = False
    running = False
    run_since = 0
    wait_since = 0
    touched = int(blob.get("lastUpdatedAt") or 0)
    fresh = touched >= now_ms - RUN_HOT_SECONDS * 1000
    status = str(blob.get("status") or "").lower()
    unfinished = int(blob.get("unfinishedRunAt") or 0)
    live_run = status == "generating" or bool(unfinished and fresh)

    if blob.get("hasBlockingPendingActions") and fresh:
        waiting = True
        running = True
        wait_since = touched or unfinished
        run_since = unfinished or touched

    if live_run:
        running = True
        run_since = unfinished or touched

    tool_cutoff = now_ms - TOOL_HOT_SECONDS * 1000
    for header in (blob.get("fullConversationHeadersOnly") or [])[-20:]:
        if not isinstance(header, dict):
            continue
        started = int(header.get("startedAtMs") or header.get("createdAtMs") or 0)
        if started and started < tool_cutoff:
            continue
        grouping = header.get("grouping") if isinstance(header.get("grouping"), dict) else {}
        shell = str(grouping.get("shellStatus") or "").lower()
        tool = str(grouping.get("toolFormerStatus") or "").lower()
        if shell in SHELL_PENDING:
            waiting = True
            running = True
            wait_since = wait_since or started or touched
            run_since = run_since or started or unfinished or touched
        elif live_run and (shell in SHELL_RUNNING or tool in TOOL_RUNNING):
            running = True
            run_since = run_since or started or unfinished or touched

    return running, waiting, run_since, wait_since


def _cursor_pending_approval(con: sqlite3.Connection, composer_id: str, now_ms: int) -> int:
    """Epoch ms du pending approval, ou 0 si aucun.

    Lit seulement les derniers bubbles (clé indexée) — un LIKE sur `value`
    scannait des milliers de blobs et figait l'UI.
    """
    cutoff = now_ms - TOOL_HOT_SECONDS * 1000
    try:
        rows = con.execute(
            "SELECT value FROM cursorDiskKV WHERE key LIKE ? ORDER BY rowid DESC LIMIT 25",
            (f"bubbleId:{composer_id}:%",),
        ).fetchall()
    except sqlite3.Error:
        return 0
    for (raw,) in rows:
        try:
            bubble = json.loads(raw or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(bubble, dict):
            continue
        created_ms = _as_ms(bubble.get("createdAt"))
        if created_ms and created_ms < cutoff:
            continue
        tool = bubble.get("toolFormerData")
        if not isinstance(tool, dict):
            continue
        if str(tool.get("status") or "").lower() not in TOOL_PENDING_STATUSES:
            continue
        if tool.get("userDecision"):
            continue
        extra = tool.get("additionalData") if isinstance(tool.get("additionalData"), dict) else {}
        review = extra.get("reviewData") if isinstance(extra.get("reviewData"), dict) else {}
        review_status = str(review.get("status") or "").lower()
        extra_status = str(extra.get("status") or "").lower()
        if review_status in REVIEW_PENDING or extra_status in REVIEW_PENDING:
            return created_ms or now_ms
    return 0


def _cursor_local() -> tuple[tuple[ActivityItem, ...], tuple[ActivityItem, ...]]:
    """Agents locaux → (en cours, en attente utilisateur / pending approval)."""
    if not STATE_DB.exists():
        return (), ()
    try:
        con = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True, timeout=0.25)
    except sqlite3.Error:
        return (), ()

    now_ms = int(time.time() * 1000)
    run_cutoff_ms = now_ms - RUN_HOT_SECONDS * 1000
    running: dict[str, ActivityItem] = {}
    waiting: dict[str, ActivityItem] = {}

    try:
        headers = con.execute(
            "SELECT composerId, lastUpdatedAt, value FROM composerHeaders "
            "WHERE IFNULL(isArchived, 0) = 0 AND IFNULL(lastUpdatedAt, 0) >= ?",
            (run_cutoff_ms,),
        ).fetchall()
    except sqlite3.Error:
        con.close()
        return (), ()

    for composer_id, last_updated, value in headers:
        try:
            header = json.loads(value or "{}")
        except (json.JSONDecodeError, TypeError):
            header = {}
        if not isinstance(header, dict):
            header = {}
        label = str(header.get("name") or "").strip() or str(composer_id)
        cid = str(composer_id)
        touched = int(last_updated or header.get("lastUpdatedAt") or 0)
        if touched < run_cutoff_ms:
            continue

        try:
            row = con.execute(
                "SELECT value FROM cursorDiskKV WHERE key = ?",
                (f"composerData:{cid}",),
            ).fetchone()
        except sqlite3.Error:
            row = None
        blob = None
        if row:
            try:
                parsed = json.loads(row[0] or "{}")
                if isinstance(parsed, dict):
                    blob = parsed
            except (json.JSONDecodeError, TypeError):
                blob = None

        run_since = wait_since = 0
        if blob is not None:
            is_running, is_waiting, run_since, wait_since = _cursor_composer_signals(blob, now_ms)
            name = str(blob.get("name") or "").strip() or label
        else:
            fresh = touched >= run_cutoff_ms
            is_waiting = bool(header.get("hasBlockingPendingActions") and fresh)
            unfinished = int(header.get("unfinishedRunAt") or 0)
            is_running = bool(is_waiting or (unfinished and fresh))
            run_since = unfinished or touched
            wait_since = touched if is_waiting else 0
            name = label

        # Pending approval : seulement si pas déjà détecté (requête bubbles coûteuse).
        if not is_waiting:
            pending_since = _cursor_pending_approval(con, cid, now_ms)
            if pending_since:
                is_waiting = True
                is_running = True
                wait_since = pending_since
                run_since = run_since or pending_since

        if is_running:
            running[cid] = ActivityItem(name, run_since)
        if is_waiting:
            waiting[cid] = ActivityItem(name, wait_since or run_since)

    con.close()
    return (
        tuple(running[k] for k in sorted(running)),
        tuple(waiting[k] for k in sorted(waiting)),
    )


def probe() -> Activity:
    claude_items, claude_waiting = _claude_live()
    cloud_items, cloud_waiting = _cursor_cloud()
    local_items, local_waiting = _cursor_local()
    cursor_by_title = {item.title: item for item in (*cloud_items, *local_items)}
    waiting_by_title = {item.title: item for item in (*cloud_waiting, *local_waiting)}
    return Activity(
        claude_items=claude_items,
        claude_waiting=claude_waiting,
        cursor_items=tuple(cursor_by_title[t] for t in sorted(cursor_by_title)),
        cursor_waiting=tuple(waiting_by_title[t] for t in sorted(waiting_by_title)),
    )
