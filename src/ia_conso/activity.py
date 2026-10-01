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
import re
import sqlite3
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from .paths import CLAUDE_PROJECTS, CLAUDE_SESSIONS, CURSOR_STATE_DB as STATE_DB

# Fenêtre de fraîcheur d'un run Cursor (lastUpdatedAt / startedAtMs outil).
RUN_HOT_SECONDS = 15 * 60
# Une session terminée reste visible cinq heures, le temps de savoir ce qui a fini depuis.
RECENT_SECONDS = 5 * 3600
# Une session coupée par une limite reste à relancer : visible jusqu'à sa relance, et au plus un
# jour après le reset de la limite. Une limite hebdo peut tomber jusqu'à sept jours plus tard.
HALTED_KEEP_SECONDS = 86400
HALTED_SCAN_SECONDS = 7 * 86400 + HALTED_KEEP_SECONDS
# Un outil commencé il y a plus longtemps n'est plus « en cours ».
TOOL_HOT_SECONDS = 5 * 60
# Le fichier de session n'est réécrit qu'au changement de statut : son âge ne dit pas si la
# session travaille. Il ne sert donc qu'à écarter un PID recyclé, d'où un seuil très large.
SESSION_STALE_SECONDS = 7 * 86400
# Queue de transcript relue à chaque changement : les jsonl montent à la centaine de Mo.
TAIL_BYTES = 256_000
# Status Claude Code qui ne consomment pas / n'attendent rien. En terminal, « shell » est un idle
# pendant qu'une commande de fond tourne.
CLAUDE_IDLE = frozenset({"idle", "shell", "done", "finished", "stopped", "exited", ""})
CLAUDE_WAITING = frozenset({"waiting"})
# En terminal, un dialogue ouvert par l'utilisateur (/model, /config…) s'écrit aussi « waiting » :
# ce n'est pas la session qui demande quelque chose.
CLAUDE_OWN_DIALOG = "dialog open"
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
# Le modèle des messages que Claude Code écrit lui-même : erreurs d'API, « No response requested. ».
SYNTHETIC_MODEL = "<synthetic>"
# Ce que Claude Code inscrit comme message de l'utilisateur quand il coupe un tour : Échap, ou
# un outil refusé (« … for tool use »).
INTERRUPTED_MARK = "[Request interrupted by user"
# Un prompt sert de titre de repli : au-delà, il ne tiendrait sur aucune ligne.
PROMPT_MAX = 120
# Les transcripts nomment le modèle en clair : on ne garde que ce qui se lit dans un menu.
MODEL_WORDS = ("opus", "sonnet", "haiku", "fable")

# (mtime → détails) par session : sonder chaque seconde ne doit relire que ce qui a changé.
_session_cache: dict[str, tuple[float, "SessionDetails"]] = {}
# session → (octets déjà examinés, ultracode armé ou non)
_ultra_cache: dict[str, tuple[int, bool | None]] = {}
# Une commande lancée en arrière-plan rend la main aussitôt : la session repasse « idle » et
# attend, parfois plusieurs minutes, la notification qui la réveillera. Ces deux traces du
# transcript la bornent ; sans elles, une session qui fait tourner une recette paraît éteinte.
# Deux façons d'y arriver : une commande lancée d'emblée en arrière-plan, ou une commande trop
# longue que Claude Code y bascule d'office.
BACKGROUND_LAUNCH = re.compile(
    r"^(?:Command running in background with ID: ?|"
    r"Command did not complete within .{1,40}? moved to the background \(ID: ?)([A-Za-z0-9]+)"
)
BACKGROUND_HINTS = ("Command running in background", "moved to the background")
BACKGROUND_NOTICE = "<task-notification>"
BACKGROUND_ID = re.compile(r"<task-id>([^<]+)</task-id>")
# Un prompt tapé par l'utilisateur, et non une notification que Claude Code lui-même envoie.
HUMAN_PROMPT = '"origin":{"kind":"human"}'
# Une commande jamais notifiée (session tuée, notification perdue) ne compte plus au-delà.
BACKGROUND_STALE_SECONDS = 2 * 3600
# session → (octets lus, lancées {id: epoch s}, notifiées, dernier prompt humain en epoch s)
_background_cache: dict[str, tuple[int, dict[str, float], set[str], float]] = {}
_agents_cache: dict[str, tuple[tuple, int]] = {}


@dataclass(frozen=True)
class SessionDetails:
    title: str = ""
    model: str = ""
    effort: str = ""
    context_tokens: int = 0
    # Le dernier prompt, pour nommer une session trop courte pour avoir reçu un titre.
    last_prompt: str = ""
    # Fin du dernier tour : le dernier message de l'assistant, en epoch ms.
    ended_ms: int = 0
    # Si ce dernier message est une erreur d'API : son code, la limite en cause et son reset.
    halt: str = ""
    limit: str = ""
    resets_ms: int = 0
    # Le dernier tour a été coupé par l'utilisateur.
    is_interrupted: bool = False


@dataclass(frozen=True)
class ActivityItem:
    """Une session qui travaille. `key` est son identité stable, jamais son titre."""

    key: str
    title: str
    since_ms: int = 0
    is_waiting: bool = False
    # Un tour est en cours : la session elle-même travaille, pas seulement ses agents ou ses tâches.
    is_busy: bool = False
    model: str = ""
    effort: str = ""
    is_ultra: bool = False
    agents: int = 0
    # Commandes lancées en arrière-plan, qui travaillent pendant que la session attend.
    background: int = 0
    # Le dernier tour a été coupé par l'utilisateur, alors qu'une commande de fond la garde affichée.
    is_interrupted: bool = False
    context_tokens: int = 0
    context_percent: float | None = None


@dataclass(frozen=True)
class FinishedItem:
    """Une session qui ne travaille plus : finie normalement, ou coupée par une erreur d'API."""

    key: str
    title: str
    ended_ms: int
    # Code de l'erreur qui a coupé le dernier tour (`rate_limit`…), vide pour une fin normale.
    halt: str = ""
    # Pour une limite : laquelle (`five_hour`, `seven_day`…) et quand elle se réarme.
    limit: str = ""
    resets_ms: int = 0
    # Coupée par l'utilisateur lui-même : Échap, ou un outil refusé.
    is_interrupted: bool = False


@dataclass(frozen=True)
class Activity:
    claude: tuple[ActivityItem, ...] = ()
    cursor: tuple[ActivityItem, ...] = ()
    # Ce qui a fini récemment : affiché pour mémoire, jamais compté dans les pastilles.
    claude_finished: tuple[FinishedItem, ...] = ()
    cursor_finished: tuple[FinishedItem, ...] = ()
    # Sessions lues à l'instant comme ayant rendu la main. Une session absente
    # n'y est pas : un fichier lu au mauvais moment ne doit pas passer pour une session finie.
    settled: frozenset[str] = frozenset()

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


def _notice_ids(text: str) -> set[str]:
    return set(BACKGROUND_ID.findall(text)) if text.lstrip().startswith(BACKGROUND_NOTICE) else set()


def _scan_background(chunk: bytes, launched: dict[str, float], notified: set[str]) -> float:
    """Relève les lancements en arrière-plan, leurs notifications de fin, et le dernier prompt humain.

    On ne retient que les traces laissées par Claude Code lui-même : un résultat d'outil qui
    commence par l'accusé de lancement, une notification en tête de message, un prompt marqué
    humain. Une session qui cite ces textes dans ses propres sorties ne fabrique ainsi aucune
    fausse tâche.
    """
    prompted = 0.0
    for line in chunk.decode("utf-8", errors="ignore").split("\n"):
        is_prompt = HUMAN_PROMPT in line
        if not is_prompt and BACKGROUND_NOTICE not in line and not any(hint in line for hint in BACKGROUND_HINTS):
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        origin = event.get("origin") if isinstance(event.get("origin"), dict) else {}
        if is_prompt and event.get("type") == "user" and origin.get("kind") == "human":
            prompted = _as_ms(event.get("timestamp")) / 1000 or time.time()
        if event.get("type") == "queue-operation":
            notified |= _notice_ids(str(event.get("content") or ""))
            continue
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        content = message.get("content")
        if isinstance(content, str):
            notified |= _notice_ids(content)
            continue
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                notified |= _notice_ids(str(block.get("text") or ""))
            elif block.get("type") == "tool_result":
                text = block.get("content")
                if isinstance(text, list):
                    text = " ".join(str(part.get("text") or "") for part in text if isinstance(part, dict))
                if match := BACKGROUND_LAUNCH.match(str(text or "")):
                    launched[match.group(1)] = _as_ms(event.get("timestamp")) / 1000 or time.time()
    return prompted


def _background_tasks(session_id: str, path: Path, size: int) -> tuple[int, int]:
    """Commandes en arrière-plan encore en cours : toutes, puis celles lancées depuis le dernier prompt.

    La seconde valeur compte celles que la session attend pour conclure ce tour-ci. Un serveur lancé lors
    d'un tour précédent tourne toujours, mais la réponse au prompt suivant n'en dépend pas.
    """
    seen, launched, notified, prompted = _background_cache.get(session_id, (0, {}, set(), 0.0))
    if not seen or size < seen:
        # Première lecture, ou fichier réécrit : on remonte depuis la fin, sans dépasser la borne.
        seen, launched, notified, prompted = max(0, size - ULTRA_SCAN_MAX), {}, set(), 0.0
    if size > seen:
        chunk = _read_at(path, seen, size)
        # Une ligne en cours d'écriture sera relue entière au passage suivant.
        cut = chunk.rfind(b"\n")
        if cut >= 0:
            prompted = _scan_background(chunk[: cut + 1], launched, notified) or prompted
            seen += cut + 1
    _background_cache[session_id] = (seen, launched, notified, prompted)
    horizon = time.time() - BACKGROUND_STALE_SECONDS
    running = [at for task, at in launched.items() if task not in notified and at >= horizon]
    return len(running), sum(1 for at in running if at >= prompted)


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
    """Titre, modèle, effort, contexte et fin du dernier tour, en une passe sur la queue du transcript.

    Une erreur d'API s'inscrit comme un message de l'assistant, au modèle `<synthetic>` et sans
    usage : elle date la fin du tour mais ne dit rien du modèle ni du contexte.
    """
    custom = ai = model = effort = prompt = halt = limit = ""
    context = ended = resets = 0
    interrupted = False
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
        if kind == "last-prompt":
            prompt = " ".join(str(event.get("lastPrompt") or "").split())[:PROMPT_MAX] or prompt
            continue
        if kind == "user" and not event.get("isSidechain") and INTERRUPTED_MARK in line:
            message = event.get("message") if isinstance(event.get("message"), dict) else {}
            content = message.get("content")
            blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
            # Un tour coupé avant toute réponse ne laisse que ce marqueur : c'est lui qui le date.
            texts = (str(block.get("text") or "") for block in blocks if isinstance(block, dict))
            if any(text.startswith(INTERRUPTED_MARK) for text in texts):
                interrupted = True
                ended = _as_ms(event.get("timestamp")) or ended
            continue
        # Un message d'agent n'est ni le modèle, ni la fin, ni la coupure de la session elle-même.
        if kind == "assistant" and not event.get("isSidechain"):
            ended = _as_ms(event.get("timestamp")) or ended
            interrupted = False
            if event.get("isApiErrorMessage"):
                quota = event.get("quotaLimits") if isinstance(event.get("quotaLimits"), dict) else {}
                halt = str(event.get("error") or "unknown")
                limit = str(quota.get("rateLimitType") or "")
                resets = _as_ms(quota.get("resetsAt"))
                continue
            halt, limit, resets = "", "", 0
            message = event.get("message") if isinstance(event.get("message"), dict) else {}
            # « No response requested. » après une interruption : synthétique lui aussi, sans usage.
            if message.get("model") == SYNTHETIC_MODEL:
                continue
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
        last_prompt=prompt,
        ended_ms=ended,
        halt=halt,
        limit=limit,
        resets_ms=resets,
        is_interrupted=interrupted,
    )


def _session_details(session_id: str, path: Path | None, mtime: float | None = None) -> SessionDetails:
    if path is None:
        return SessionDetails()
    try:
        mtime = path.stat().st_mtime if mtime is None else mtime
    except OSError:
        return SessionDetails()
    cached = _session_cache.get(session_id)
    if cached and cached[0] == mtime:
        return cached[1]
    details = _read_session(path)
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


def _claude_sessions() -> tuple[tuple[ActivityItem, ...], frozenset[str]]:
    """Sessions Claude Code vivantes et non idle, une ligne par session, et celles qui ont rendu la main.

    Une session se reconnaît à son `sessionId` : c'est lui qui la relie à son transcript, donc à
    la liste des sessions terminées, et deux process qui reprennent la même session n'en font qu'une.
    """
    if not CLAUDE_SESSIONS.is_dir():
        return (), frozenset()
    items: dict[str, ActivityItem] = {}
    settled: set[str] = set()
    busy: set[str] = set()
    stale_before = time.time() - SESSION_STALE_SECONDS
    try:
        paths = sorted(CLAUDE_SESSIONS.glob("*.json"))
    except OSError:
        return (), frozenset()
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
        if status in CLAUDE_WAITING and blob.get("waitingFor") == CLAUDE_OWN_DIALOG:
            status = "idle"
        session_id = str(blob.get("sessionId") or "").strip()
        cwd = str(blob.get("cwd") or "").strip()
        agents = _workflow_agents(session_id) + _loose_agents(session_id)
        transcript = _transcript(session_id, cwd)
        try:
            size = transcript.stat().st_size if transcript else 0
        except OSError:
            size = 0
        background, pending = _background_tasks(session_id, transcript, size) if size else (0, 0)
        key = session_id or f"pid:{pid}"
        # Claude Code garde la session « busy » tant que ses agents ou son workflow tournent : son
        # « idle » est la vraie fin du tour, que les agents comptés ici soient retombés ou non. Il
        # la rend « idle » en revanche pendant une commande lancée en fond pour ce tour : la session
        # attend son résultat pour conclure, elle n'a pas fini.
        if status in CLAUDE_IDLE and not pending:
            settled.add(key)
        elif status not in CLAUDE_WAITING:
            busy.add(key)
        details = _session_details(session_id, transcript)
        # « idle » veut dire « attend l'utilisateur », pas « ne fait rien » : une session qui a rendu
        # la main pendant que ses agents ou une commande de fond travaillent est encore à l'œuvre.
        # Coupée par une erreur, elle est à relancer, et sa ligne passe dans les terminées.
        if status in CLAUDE_IDLE and not (agents or (background and not details.halt)):
            continue
        if key in items:
            continue
        started = _as_ms(blob.get("startedAt") or 0)
        items[key] = ActivityItem(
            key=key,
            title=details.title or str(blob.get("name") or "").strip() or details.last_prompt or path.stem,
            since_ms=_as_ms(blob.get("statusUpdatedAt") or started) or started,
            is_waiting=status in CLAUDE_WAITING,
            is_busy=status not in CLAUDE_IDLE and status not in CLAUDE_WAITING,
            model=details.model,
            effort=details.effort,
            is_ultra=_ultra_state(session_id, transcript, size) if size else False,
            agents=agents,
            background=background,
            is_interrupted=details.is_interrupted,
            context_tokens=details.context_tokens,
        )
    # Deux process sur la même session : si l'un travaille, elle n'a pas rendu la main.
    return tuple(items.values()), frozenset(settled - busy)


def _transcripts(since: float) -> list[tuple[Path, float]]:
    """(transcript, mtime) des sessions modifiées depuis `since` (epoch s) — les agents sont plus bas."""
    found: list[tuple[Path, float]] = []
    try:
        projects = [entry.path for entry in os.scandir(CLAUDE_PROJECTS) if entry.is_dir()]
    except OSError:
        return found
    for project in projects:
        try:
            with os.scandir(project) as entries:
                for entry in entries:
                    if entry.name.endswith(".jsonl") and entry.is_file() and (mtime := entry.stat().st_mtime) >= since:
                        found.append((Path(entry.path), mtime))
        except OSError:
            continue
    return found


def _claude_finished(running: set[str]) -> tuple[FinishedItem, ...]:
    """Sessions qui ne travaillent plus : finies depuis moins de cinq heures, ou coupées et pas encore relancées.

    Le transcript reste quand la session se ferme, c'est donc lui qui fait foi, et la fin d'une
    session est celle de son dernier tour : la date du fichier bouge encore à sa reprise. Une
    session en cours n'y figure jamais ; une session coupée en tête, parce qu'elle est à relancer.
    """
    now_ms = int(time.time() * 1000)
    finished: dict[str, FinishedItem] = {}
    for path, mtime in _transcripts(now_ms / 1000 - HALTED_SCAN_SECONDS):
        session_id = path.stem
        if session_id in running:
            continue
        details = _session_details(session_id, path, mtime)
        if not details.ended_ms:
            continue
        if details.halt:
            shown_until = max(details.ended_ms, details.resets_ms) + HALTED_KEEP_SECONDS * 1000
        else:
            shown_until = details.ended_ms + RECENT_SECONDS * 1000
        if shown_until < now_ms or details.ended_ms <= finished.get(session_id, FinishedItem("", "", 0)).ended_ms:
            continue
        finished[session_id] = FinishedItem(
            key=session_id,
            title=details.title or details.last_prompt or "session",
            ended_ms=details.ended_ms,
            halt=details.halt,
            limit=details.limit,
            resets_ms=details.resets_ms,
            is_interrupted=details.is_interrupted,
        )
    return tuple(sorted(finished.values(), key=lambda item: (not item.halt, -item.ended_ms)))


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


def _cursor_agents() -> tuple[tuple[ActivityItem, ...], tuple[FinishedItem, ...]]:
    """Agents Cursor locaux, en cours puis terminés depuis moins de cinq heures, identifiés par leur composerId.

    Un composer qui n'a pas bougé depuis la fenêtre de fraîcheur ne peut plus être en cours : il
    est terminé sans qu'on ait à charger son composerData. Un brouillon, ou un composer qui n'a pas
    encore reçu de nom, n'a jamais rien fait.
    """
    if not STATE_DB.exists():
        return (), ()
    try:
        con = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True, timeout=0.25)
    except sqlite3.Error:
        return (), ()
    now_ms = int(time.time() * 1000)
    cutoff = now_ms - RUN_HOT_SECONDS * 1000
    recent = now_ms - RECENT_SECONDS * 1000
    items: list[ActivityItem] = []
    finished: list[FinishedItem] = []
    try:
        headers = con.execute(
            "SELECT composerId, lastUpdatedAt, value FROM composerHeaders "
            "WHERE IFNULL(isArchived, 0) = 0 AND IFNULL(lastUpdatedAt, 0) >= ?",
            (recent,),
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
            name = str(header.get("name") or "").strip()
            if touched < recent:
                continue
            if touched < cutoff:
                # Bloqué sur une approbation, il n'a rien fini : il attend, hors de la fenêtre.
                if name and not header.get("isDraft") and not header.get("hasBlockingPendingActions"):
                    finished.append(FinishedItem(key=cid, title=name, ended_ms=touched))
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
                name = str(blob.get("name") or "").strip() or name
                if name and not header.get("isDraft"):
                    finished.append(FinishedItem(key=cid, title=name, ended_ms=touched))
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
                    is_busy=not waiting,
                    model=("auto" if model == "default" else model) + (" max" if config.get("maxMode") else ""),
                    agents=int(header.get("numSubComposers") or 0),
                    context_percent=float(context) if isinstance(context, (int, float)) else None,
                )
            )
    except sqlite3.Error:
        return (), ()
    finally:
        con.close()
    return (
        tuple(sorted(items, key=lambda item: item.key)),
        tuple(sorted(finished, key=lambda item: -item.ended_ms)),
    )


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
                    is_busy=not waiting,
                )
            )
    return tuple(items)


def probe() -> Activity:
    claude, settled = _claude_sessions()
    local, ended = _cursor_agents()
    cursor = {item.key: item for item in (*_cursor_cloud(), *local)}
    cursor_finished = tuple(item for item in ended if item.key not in cursor)
    return Activity(
        claude=claude,
        cursor=tuple(cursor[key] for key in sorted(cursor)),
        claude_finished=_claude_finished({item.key for item in claude}),
        cursor_finished=cursor_finished,
        settled=settled | {item.key for item in cursor_finished},
    )
