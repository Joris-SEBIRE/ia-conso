"""Ce que les API de conso ont rendu, déjà aplati pour l'affichage.

Claude et Cursor rendent la même forme : un compte, une fenêtre principale qui se réarme, des
fenêtres secondaires, des crédits hors forfait. Un seul type de vue les porte donc tous les deux,
et le menu n'a qu'une façon de les dessiner.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

CLAUDE = "claude"
CURSOR = "cursor"


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Account:
    """L'identité derrière le token : la personne, pas l'organisation."""

    name: str
    email: str
    plan: str
    org_id: str = ""
    org_name: str = ""


@dataclass(frozen=True)
class Window:
    """Une fenêtre de conso qui se réarme : session 5 h, semaine, période de facturation."""

    label: str
    percent: float
    resets_at: datetime | None
    is_session: bool = False
    # Montants quand l'API les donne (Cursor toujours, Anthropic seulement sur certains plans).
    used: float | None = None
    cap: float | None = None
    currency: str = ""


@dataclass(frozen=True)
class Extra:
    """Conso hors forfait : crédits Anthropic, dépassement Cursor."""

    used: float
    cap: float
    currency: str
    percent: float | None
    is_enabled: bool
    resets_at: datetime | None = None
    disabled_reason: str = ""


@dataclass(frozen=True)
class Refill:
    """Une remise à zéro que l'on peut déclencher soi-même, depuis Claude Code.

    Deux formes : le reset de la session 5 h, décompté du quota hebdomadaire, et des recharges
    nominatives offertes pour un temps. Les deux sont facultatives et n'existent que sur les
    comptes qui y ont droit.
    """

    label: str
    is_available: bool = False
    left: int | None = None
    total: int | None = None
    available_at: datetime | None = None
    ends_at: datetime | None = None
    needs_limit: bool = False


@dataclass(frozen=True)
class AccountView:
    """Un compte affiché dans le menu : une organisation Claude, ou le compte Cursor."""

    key: str
    provider: str = CLAUDE
    name: str = ""
    plan: str = ""
    email: str = ""
    is_active: bool = False
    session: Window | None = None
    weekly: Window | None = None
    scoped: tuple[Window, ...] = ()
    extra: Extra | None = None
    refills: tuple[Refill, ...] = ()
    breakdown: tuple[tuple[str, float], ...] = ()
    fetched_at: datetime | None = None
    error: str = ""

    @property
    def has_figures(self) -> bool:
        return bool(self.session or self.weekly or self.scoped or self.extra)


@dataclass(frozen=True)
class Snapshot:
    account: Account | None = None
    views: tuple[AccountView, ...] = ()
    # Dernière lecture réussie, pour la fraîcheur affichée.
    fetched_at: datetime | None = None
    # Dernière tentative, réussie ou non : c'est elle qui cadence le cycle suivant.
    attempted_at: datetime | None = None
    error: str = ""
    token_origin: str = ""
    retry_after: int | None = None

    @property
    def active(self) -> AccountView | None:
        return next((view for view in self.views if view.provider == CLAUDE and view.is_active), None)

    def of(self, provider: str) -> tuple[AccountView, ...]:
        return tuple(view for view in self.views if view.provider == provider)
