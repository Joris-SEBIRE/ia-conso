"""Ce que l'API de conso Claude a rendu, déjà aplati pour l'affichage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Account:
    name: str
    email: str
    plan: str


@dataclass(frozen=True)
class Window:
    label: str
    percent: float
    resets_at: datetime | None
    is_session: bool = False


@dataclass(frozen=True)
class Extra:
    used: float
    cap: float
    currency: str
    percent: float | None
    is_enabled: bool


@dataclass(frozen=True)
class Snapshot:
    account: Account | None = None
    session: Window | None = None
    weekly: Window | None = None
    scoped: tuple[Window, ...] = ()
    extra: Extra | None = None
    breakdown: tuple[tuple[str, float], ...] = ()
    fetched_at: datetime | None = None
    error: str = ""
    token_origin: str = ""
    retry_after: int | None = None
