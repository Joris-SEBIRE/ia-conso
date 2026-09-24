"""Configuration utilisateur, dans ~/.config/ia-conso/config.json."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from types import UnionType
from typing import Union, get_args, get_origin, get_type_hints

from .paths import CONFIG_PATH


def _accepted(hint) -> tuple:
    if get_origin(hint) in (Union, UnionType):
        return tuple(t for arm in get_args(hint) for t in _accepted(arm))
    origin = get_origin(hint) or hint
    return (origin,) if isinstance(origin, type) else ()


def _keep(value, hint) -> bool:
    """Le fichier est modifiable à la main : une valeur du mauvais type est écartée."""
    accepted = _accepted(hint)
    if not accepted:
        return True
    if isinstance(value, bool) and bool not in accepted:
        return False
    return isinstance(value, accepted)


@dataclass
class Config:
    # L'endpoint de conso est gratuit, mais sans User-Agent Claude Code il tombe en 429.
    # Une minute laisse le pourcentage suivre sans bombarder.
    refresh_seconds: int = 60

    @classmethod
    def load(cls) -> "Config":
        known = {f.name for f in fields(cls)}
        hints = get_type_hints(cls)
        data: dict = {}
        if CONFIG_PATH.exists():
            try:
                raw = json.loads(CONFIG_PATH.read_text() or "{}")
            except (json.JSONDecodeError, OSError):
                raw = {}
            if isinstance(raw, dict):
                data = {k: v for k, v in raw.items() if k in known and _keep(v, hints[k])}
        cfg = cls(**data)
        if set(data) != known:
            cfg.save()
        return cfg

    def save(self) -> None:
        """Écrit par un temporaire puis un renommage : le fil de fond relit ce fichier."""
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
        try:
            temporary.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False) + "\n")
            os.replace(temporary, CONFIG_PATH)
        except OSError:
            temporary.unlink(missing_ok=True)
