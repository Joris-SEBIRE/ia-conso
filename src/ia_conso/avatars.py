"""Photos de profil via Gravatar : Claude n'expose pas d'avatar dans l'OAuth."""

from __future__ import annotations

import hashlib
import time
import urllib.error
import urllib.request
from pathlib import Path

from Cocoa import (
    NSBezierPath,
    NSCompositingOperationSourceOver,
    NSImage,
    NSMakeRect,
    NSMakeSize,
    NSZeroRect,
)

CACHE_DIR = Path.home() / "Library" / "Caches" / "IAConso" / "avatars"
MAX_AGE = 14 * 86400
SIZE = 22.0


def gravatar_url(email: str, size: int = 128) -> str:
    digest = hashlib.md5(email.strip().lower().encode()).hexdigest()
    return f"https://www.gravatar.com/avatar/{digest}?s={size}&d=404"


class Avatars:
    def __init__(self) -> None:
        self.rendered: dict[tuple[str, float], object] = {}

    def path_for(self, url: str) -> Path:
        return CACHE_DIR / (hashlib.sha1(url.encode()).hexdigest()[:16] + ".img")

    def prefetch(self, emails: set[str]) -> None:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        for email in emails:
            if not email:
                continue
            url = gravatar_url(email)
            target = self.path_for(url)
            if target.exists() and (time.time() - target.stat().st_mtime) < MAX_AGE:
                continue
            try:
                request = urllib.request.Request(url, headers={"User-Agent": "IA-Conso"})
                with urllib.request.urlopen(request, timeout=10) as response:
                    if getattr(response, "status", 200) == 404:
                        continue
                    data = response.read()
            except (urllib.error.URLError, OSError, ValueError):
                continue
            if not data:
                continue
            temporary = target.with_suffix(".part")
            temporary.write_bytes(data)
            temporary.replace(target)
            for key in [k for k in self.rendered if k[0] == url]:
                del self.rendered[key]

    def image(self, email: str, size: float = SIZE):
        if not email:
            return None
        url = gravatar_url(email)
        if (url, size) in self.rendered:
            return self.rendered[(url, size)]
        source = NSImage.alloc().initWithContentsOfFile_(str(self.path_for(url)))
        if source is None:
            return None
        box = NSMakeRect(0, 0, size, size)
        circle = NSImage.alloc().initWithSize_(NSMakeSize(size, size))
        circle.lockFocus()
        NSBezierPath.bezierPathWithOvalInRect_(box).addClip()
        source.drawInRect_fromRect_operation_fraction_(box, NSZeroRect, NSCompositingOperationSourceOver, 1.0)
        circle.unlockFocus()
        self.rendered[(url, size)] = circle
        return circle
