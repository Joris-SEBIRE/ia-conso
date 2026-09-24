"""Signaux locaux : qui travaille en ce moment, sur quel modèle, et avec combien d'agents.

Claude Code : `~/.claude/sessions/<pid>.json` porte un `status` (`idle` / `busy` / `waiting` =
en attente d'une action utilisateur). Le reste — titre affiché dans l'IDE, modèle, effort,
contexte occupé, sous-agents en vol — se lit dans le transcript de la session et dans le journal
de ses agents, tous deux déjà sur le disque.
Cursor : `composerHeaders` porte le nom, le contexte et le blocage ; `composerData` ajoute le
modèle et l'état du run en cours.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from .paths import CLAUDE_PROJECTS, CLAUDE_SESSIONS, CURSOR_STATE_DB as STATE_DB

# Fenêtre de fraîcheur d'un run Cursor (lastUpdatedAt / startedAtMs outil).
RUN_HOT_SECONDS = 15 * 60
# Un outil commencé il y a plus longtemps n'est plus « en cours ».
TOOL_HOT_SECONDS = 5 * 60
# Le fichier de session n'est réécrit qu'au changement de statut : son âge ne dit pas si la
# session travaille. Il ne sert donc qu'à écarter un PID recyclé, d'où un seuil très large.
SESSION_STALE_SECONDS = 7 * 86400
# Queue de transcript relue à chaque changement : les jsonl montent à la centaine de Mo.
TAIL_BYTES = 256_000
# Status Claude Code qui ne consomment pas / n'attendent rien.
CLAUDE_IDLE = frozenset({"idle", "done", "finished", "stopped", "exited", ""})
CLAUDE_WAITING = frozenset({"waiting"})
SHELL_PENDING = frozenset({"pending"})
SHELL_RUNNING = frozenset({"running", "loading", "pending"})
TOOL_RUNNING = frozenset({"loading"})
REVIEW_PENDING = frozenset({"requested", "pending"})
TOOL_PENDING_STATUSES = frozenset({"loading", "pending"})
# Un journal ou un transcript d'agent plus froid que ça décrit un travail abandonné.
AGENT_HOT_SECONDS = 15 * 60
# Claude Code inscrit dans son propre transcript un attachment `ultra_effort_enter` quand
# ultracode est armé, `ultra_effort_exit` quand il retombe, et relit le dernier pour retrouver
# son état. On applique la même règle. Le marqueur n'est réécrit que tous les dix prompts, donc
# il est souvent loin de la fin : on remonte une fois, borné, puis on ne lit que ce qui s'ajoute.
ULTRA_ENTER = b'"ultra_effort_enter"'
ULTRA_EXIT = b'"ultra_effort_exit"'
ULTRA_SCAN_MAX = 8 << 20
# Un marqueur peut être à cheval sur deux lectures successives.
ULTRA_OVERLAP = 256

# Un agent de fond n'a pas de journal qui le clôt : on le tient pour vivant tant que son
# transcript bouge. Il peut donc disparaître un instant s'il réfléchit longtemps sans écrire.
AGENT_IDLE_SECONDS = 120
AGENT_CLOSED = frozenset({"result", "failed", "skipped", "cancelled"})
# Les transcripts nomment le modèle en clair : on ne garde que ce qui se lit dans un menu.
MODEL_WORDS = ("opus", "sonnet", "haiku", "fable")

# (mtime → détails) par session : sonder chaque seconde ne doit relire que ce qui a changé.
_session_cache: dict[str, tuple[float, "SessionDetails"]] = {}
# session → (octets déjà examinés, ultracode armé ou non)
_ultra_cache: dict[str, tuple[int, bool | None]] = {}
_agents_cache: dict[str, tuple[tuple, int]] = {}


@dataclass(frozen=True)
class SessionDetails:
    title: str = ""
    model: str = ""
    effort: str = ""
    context_tokens: int = 0
    is_ultra: bool = False


@dataclass(frozen=True)
class ActivityItem:
    """Une session qui travaille. `key` est son identité stable, jamais son titre."""

    key: str
    title: str
    since_ms: int = 0
    is_waiting: bool = False
    model: str = ""
    effort: str = ""
    is_ultra: bool = False
    agents: int = 0
    context_tokens: int = 0
    context_percent: float | None = None


@dataclass(frozen=True)
class Activity:
    claude: tuple[ActivityItem, ...] = ()
    cursor: tuple[ActivityItem, ...] = ()

    @property
    def items(self) -> tuple[ActivityItem, ...]:
        return self.claude + self.cursor

    @property
    def running_count(self) -> int:
        """Sessions qui travaillent — une par ligne du menu, pour que la pastille se recoupe.

        Les agents ne s'ajoutent pas à ce compte : ils appartiennent à une session et s'affichent
        sur sa ligne. Une pastille qui mélangerait les deux ne se retrouverait dans aucun détail.
        """
        return sum(1 for item in self.items if not item.is_waiting)

    @property
    def agent_count(self) -> int:
        """Agents en vol, toutes sessions confondues, y compris celles qui attendent une réponse."""
        return sum(item.agents for item in self.items)

    @property
    def waiting_count(self) -> int:
        return sum(1 for item in self.items if item.is_waiting)


def _as_ms(value) -> int:
    """Epoch ms ; accepte aussi des epoch secondes ou ISO."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        n = int(value)
        return n * 1000 if 0 < n < 10_000_000_000 else n
    if isinstance(value, str) and value:
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            return 0
    return 0


def _tail(path: Path, size: int = TAIL_BYTES) -> str:
    """Les derniers octets d'un fichier, sans charger le reste en mémoire.

    Le découpage en lignes se fait ensuite sur `\n` seul : `splitlines()` couperait aussi sur
    U+2028, présent dans le texte que les agents rendent, et casserait la ligne JSON en deux.
    """
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - size))
            return handle.read().decode("utf-8", errors="ignore")
    except OSError:
        return ""


def _read_at(path: Path, start: int, end: int) -> bytes:
    try:
        with path.open("rb") as handle:
            handle.seek(max(0, start))
            return handle.read(max(0, end - max(0, start)))
    except OSError:
        return b""


def _last_ultra(chunk: bytes) -> bool | None:
    """Le dernier marqueur d'un extrait : armé, retombé, ou aucun des deux."""
    enter, exit_ = chunk.rfind(ULTRA_ENTER), chunk.rfind(ULTRA_EXIT)
    if enter < 0 and exit_ < 0:
        return None
    return enter > exit_


def _ultra_state(session_id: str, path: Path, size: int) -> bool:
    """Ultracode est-il armé sur cette session ? Le dernier marqueur du transcript fait foi."""
    seen, active = _ultra_cache.get(session_id, (0, None))
    if seen and size >= seen:
        if size > seen:
            found = _last_ultra(_read_at(path, seen - ULTRA_OVERLAP, size))
            if found is not None:
                active = found
            _ultra_cache[session_id] = (size, active)
        return bool(active)
    # Première lecture, ou fichier réécrit : on remonte depuis la fin, sans dépasser la borne.
    active = _last_ultra(_read_at(path, size - ULTRA_SCAN_MAX, size))
    _ultra_cache[session_id] = (size, active)
    return bool(active)


def _model_label(name: str) -> str:
    """« claude-opus-5 » → « Opus 5 » ; un identifiant inconnu est rendu tel quel."""
    text = str(name or "").removeprefix("claude-")
    for word in MODEL_WORDS:
        if text.startswith(word):
            version = text[len(word) :].strip("-").replace("-", ".").split("[")[0]
            return f"{word.capitalize()} {version}".strip()
    return text.split("[")[0]


def _transcript(session_id: str, cwd: str) -> Path | None:
    if not session_id:
        return None
    root = CLAUDE_PROJECTS / (cwd or "").strip().replace("/", "-") if cwd else None
    path = (root / f"{session_id}.jsonl") if root else None
    if path is not None and path.is_file():
        return path
    try:
        matches = list(CLAUDE_PROJECTS.glob(f"*/{session_id}.jsonl")) if CLAUDE_PROJECTS.is_dir() else []
    except OSError:
        return None
    return matches[0] if matches else None


def _read_session(path: Path) -> SessionDetails:
    """Titre, modèle, effort, contexte et agents ouverts, en une passe sur la queue du transcript."""
    custom = ai = model = effort = ""
    context = 0
    for line in _tail(path).split("\n"):
        if "{" not in line:
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "custom-title":
            custom = str(event.get("customTitle") or "").strip() or custom
            continue
        if kind == "ai-title":
            ai = str(event.get("aiTitle") or "").strip() or ai
            continue
        if kind == "assistant":
            message = event.get("message") if isinstance(event.get("message"), dict) else {}
            model = str(message.get("model") or "") or model
            effort = str(event.get("perTurnEffort") or event.get("effort") or "") or effort
            usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
            if usage:
                context = sum(
                    int(usage.get(key) or 0)
                    for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
                )
    return SessionDetails(
        title=custom or ai,
        model=_model_label(model),
        effort=effort,
        context_tokens=context,
    )


def _session_details(session_id: str, cwd: str) -> SessionDetails:
    path = _transcript(session_id, cwd)
    if path is None:
        return SessionDetails()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return SessionDetails()
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    ultra = _ultra_state(session_id, path, size)
    cached = _session_cache.get(session_id)
    if cached and cached[0] == mtime:
        return replace(cached[1], is_ultra=ultra)
    details = replace(_read_session(path), is_ultra=ultra)
    if not details.title and cached:
        # Le titre peut être plus vieux que la queue relue : on garde le dernier connu.
        details = replace(details, title=cached[1].title)
    _session_cache[session_id] = (mtime, details)
    return details


def _workflow_agents(session_id: str) -> int:
    """Agents d'un workflow encore en vol : `started` sans `result` ni `failed` dans le journal."""
    if not session_id:
        return 0
    try:
        hot = time.time() - AGENT_HOT_SECONDS
        journals = sorted(
            path
            for path in CLAUDE_PROJECTS.glob(f"*/{session_id}/subagents/workflows/*/journal.jsonl")
            # Un workflow interrompu laisse ses agents « démarrés » pour toujours : seul un
            # journal qui vit encore décrit des agents réellement en vol.
            if path.stat().st_mtime >= hot
        )
    except OSError:
        return 0
    if not journals:
        return 0
    try:
        stamps = tuple((str(path), path.stat().st_mtime) for path in journals)
    except OSError:
        return 0
    cached = _agents_cache.get(session_id)
    if cached and cached[0] == stamps:
        return cached[1]
    running = 0
    for path in journals:
        started: set[str] = set()
        ended: set[str] = set()
        for line in _tail(path, TAIL_BYTES).split("\n"):
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(event, dict):
                continue
            agent = str(event.get("agentId") or "")
            kind = str(event.get("type") or "")
            if not agent:
                continue
            if kind == "started":
                started.add(agent)
            elif kind in AGENT_CLOSED:
                # Un type inconnu ne ferme rien : mieux vaut compter un agent de trop qu'en perdre.
                ended.add(agent)
        running += len(started - ended)
    _agents_cache[session_id] = (stamps, running)
    return running


def _loose_agents(session_id: str) -> int:
    """Agents lancés hors workflow : leur transcript est le seul signe qu'ils travaillent encore."""
    if not session_id:
        return 0
    hot = time.time() - AGENT_IDLE_SECONDS
    try:
        return sum(
            1
            for path in CLAUDE_PROJECTS.glob(f"*/{session_id}/subagents/agent-*.jsonl")
            if path.stat().st_mtime >= hot
        )
    except OSError:
        return 0


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError, PermissionError):
        return False


def _claude_sessions() -> tuple[ActivityItem, ...]:
    """Sessions Claude Code vivantes et non idle, une ligne par session."""
    if not CLAUDE_SESSIONS.is_dir():
        return ()
    items: list[ActivityItem] = []
    stale_before = time.time() - SESSION_STALE_SECONDS
    try:
        paths = sorted(CLAUDE_SESSIONS.glob("*.json"))
    except OSError:
        return ()
    for path in paths:
        if not path.stem.isdigit():
            continue
        try:
            if path.stat().st_mtime < stale_before:
                continue
            blob = json.loads(path.read_text() or "{}")
        except (OSError, json.JSONDecodeError, UnicodeError):
            continue
        if not isinstance(blob, dict):
            continue
        try:
            pid = int(blob.get("pid") or path.stem)
        except (TypeError, ValueError):
            continue
        if not _pid_alive(pid):
            continue
        status = str(blob.get("status") or "").strip().lower()
        if status in CLAUDE_IDLE:
            continue
        session_id = str(blob.get("sessionId") or "").strip()
        details = _session_details(session_id, str(blob.get("cwd") or "").strip())
        started = _as_ms(blob.get("startedAt") or 0)
        items.append(
            ActivityItem(
                key=str(pid),
                title=details.title or str(blob.get("name") or "").strip() or path.stem,
                since_ms=_as_ms(blob.get("statusUpdatedAt") or started) or started,
                is_waiting=status in CLAUDE_WAITING,
                model=details.model,
                effort=details.effort,
                is_ultra=details.is_ultra,
                agents=_workflow_agents(session_id) + _loose_agents(session_id),
                context_tokens=details.context_tokens,
            )
        )
    return tuple(items)


def _cursor_run_signals(blob: dict, header: dict, now_ms: int) -> tuple[bool, bool, int, int]:
    """(en cours, en attente, since_run_ms, since_wait_ms) d'après le header et composerData."""
    waiting = running = False
    run_since = wait_since = 0
    touched = _as_ms(blob.get("lastUpdatedAt") or header.get("lastUpdatedAt") or 0)
    fresh = touched >= now_ms - RUN_HOT_SECONDS * 1000
    unfinished = _as_ms(blob.get("unfinishedRunAt") or header.get("unfinishedRunAt") or 0)
    live_run = str(blob.get("status") or "").lower() == "generating" or bool(unfinished and fresh)

    # Le drapeau de blocage vit dans le header ; composerData ne le porte pas.
    if (header.get("hasBlockingPendingActions") or blob.get("hasBlockingPendingActions")) and fresh:
        waiting = running = True
        wait_since = touched or unfinished
        run_since = unfinished or touched

    if live_run:
        running = True
        run_since = unfinished or touched

    tool_cutoff = now_ms - TOOL_HOT_SECONDS * 1000
    for entry in (blob.get("fullConversationHeadersOnly") or [])[-20:]:
        if not isinstance(entry, dict):
            continue
        started = _as_ms(entry.get("startedAtMs") or entry.get("createdAtMs") or 0)
        if started and started < tool_cutoff:
            continue
        grouping = entry.get("grouping") if isinstance(entry.get("grouping"), dict) else {}
        shell = str(grouping.get("shellStatus") or "").lower()
        tool = str(grouping.get("toolFormerStatus") or "").lower()
        if shell in SHELL_PENDING:
            waiting = running = True
            wait_since = wait_since or started or touched
            run_since = run_since or started or unfinished or touched
        elif live_run and (shell in SHELL_RUNNING or tool in TOOL_RUNNING):
            running = True
            run_since = run_since or started or unfinished or touched
    return running, waiting, run_since, wait_since


def _cursor_pending_approval(con: sqlite3.Connection, composer_id: str, now_ms: int) -> int:
    """Epoch ms du pending approval, ou 0 si aucun.

    La clé est indexée, mais SQLite n'utilise pas cet index pour un `LIKE` : il faut une
    comparaison par bornes, sans quoi la requête balaie toute la table (des centaines de Mo).
    Le tri reste sur `rowid` : le suffixe de la clé est un UUID, trier dessus rendrait 25 bubbles
    au hasard plutôt que les 25 derniers écrits.
    """
    cutoff = now_ms - TOOL_HOT_SECONDS * 1000
    prefix = f"bubbleId:{composer_id}:"
    try:
        rows = con.execute(
            "SELECT value FROM cursorDiskKV WHERE key >= ? AND key < ? ORDER BY rowid DESC LIMIT 25",
            (prefix, prefix + "￿"),
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
        if not isinstance(tool, dict) or str(tool.get("status") or "").lower() not in TOOL_PENDING_STATUSES:
            continue
        if tool.get("userDecision"):
            continue
        extra = tool.get("additionalData") if isinstance(tool.get("additionalData"), dict) else {}
        review = extra.get("reviewData") if isinstance(extra.get("reviewData"), dict) else {}
        if str(review.get("status") or "").lower() in REVIEW_PENDING or str(extra.get("status") or "").lower() in REVIEW_PENDING:
            return created_ms or now_ms
    return 0


def _cursor_agents() -> tuple[ActivityItem, ...]:
    """Agents Cursor locaux : une ligne par composer, identifiée par son composerId."""
    if not STATE_DB.exists():
        return ()
    try:
        con = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True, timeout=0.25)
    except sqlite3.Error:
        return ()
    now_ms = int(time.time() * 1000)
    cutoff = now_ms - RUN_HOT_SECONDS * 1000
    items: list[ActivityItem] = []
    try:
        headers = con.execute(
            "SELECT composerId, lastUpdatedAt, value FROM composerHeaders "
            "WHERE IFNULL(isArchived, 0) = 0 AND IFNULL(lastUpdatedAt, 0) >= ?",
            (cutoff,),
        ).fetchall()
        for composer_id, last_updated, value in headers:
            try:
                header = json.loads(value or "{}")
            except (json.JSONDecodeError, TypeError):
                header = {}
            if not isinstance(header, dict):
                header = {}
            cid = str(composer_id)
            touched = _as_ms(last_updated or header.get("lastUpdatedAt") or 0)
            if touched < cutoff:
                continue
            blob = {}
            try:
                row = con.execute("SELECT value FROM cursorDiskKV WHERE key = ?", (f"composerData:{cid}",)).fetchone()
                if row:
                    parsed = json.loads(row[0] or "{}")
                    blob = parsed if isinstance(parsed, dict) else {}
            except (sqlite3.Error, json.JSONDecodeError, TypeError):
                blob = {}

            running, waiting, run_since, wait_since = _cursor_run_signals(blob, header, now_ms)
            if not waiting:
                pending_since = _cursor_pending_approval(con, cid, now_ms)
                if pending_since:
                    running = waiting = True
                    wait_since = pending_since
                    run_since = run_since or pending_since
            if not running:
                continue
            config = blob.get("modelConfig") if isinstance(blob.get("modelConfig"), dict) else {}
            model = str(config.get("modelName") or "")
            context = header.get("contextUsagePercent")
            items.append(
                ActivityItem(
                    key=cid,
                    title=str(blob.get("name") or header.get("name") or "").strip() or cid,
                    since_ms=(wait_since or run_since) if waiting else run_since,
                    is_waiting=waiting,
                    model=("auto" if model == "default" else model) + (" max" if config.get("maxMode") else ""),
                    agents=int(header.get("numSubComposers") or 0),
                    context_percent=float(context) if isinstance(context, (int, float)) else None,
                )
            )
    except sqlite3.Error:
        return ()
    finally:
        con.close()
    return tuple(sorted(items, key=lambda item: item.key))


def _agent_state(agent: dict) -> tuple[bool, bool]:
    """(vivant, en attente) d'un agent cloud Cursor."""
    status = str(agent.get("status") or agent.get("state") or "").lower()
    if status in AGENT_CLOSED or status in ("done", "finished", "complete", "completed", "canceled", "archived"):
        return False, False
    if agent.get("isArchived") or agent.get("archived"):
        return False, False
    waiting = bool(agent.get("hasBlockingPendingActions")) or status in (
        "waiting",
        "needs_input",
        "blocked",
        "pending_user",
        "awaiting_input",
    )
    return True, waiting


def _cursor_cloud() -> tuple[ActivityItem, ...]:
    """Agents cloud (Background Composer), listés dans la base d'état comme un simple JSON."""
    if not STATE_DB.exists():
        return ()
    try:
        con = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True, timeout=0.25)
        try:
            rows = con.execute(
                "SELECT value FROM ItemTable WHERE key LIKE 'cloudAgentRepository.agents.%'"
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return ()
    items: list[ActivityItem] = []
    for (raw,) in rows:
        try:
            agents = json.loads(raw or "[]")
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(agents, list):
            continue
        for agent in agents:
            if not isinstance(agent, dict):
                continue
            alive, waiting = _agent_state(agent)
            if not alive:
                continue
            key = str(agent.get("id") or agent.get("agentId") or agent.get("name") or len(items))
            title = next((str(agent[k]).strip() for k in ("name", "title", "summary") if agent.get(k)), "agent cloud")
            items.append(
                ActivityItem(
                    key=key,
                    title=title,
                    since_ms=_as_ms(agent.get("updatedAt") or agent.get("createdAt") or 0),
                    is_waiting=waiting,
                )
            )
    return tuple(items)


def probe() -> Activity:
    cursor = {item.key: item for item in (*_cursor_cloud(), *_cursor_agents())}
    return Activity(claude=_claude_sessions(), cursor=tuple(cursor[key] for key in sorted(cursor)))
