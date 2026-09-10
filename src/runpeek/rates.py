"""Rate cards: immutable pricing versions with an effective window.

Resolution picks the card effective at the attempt's start time. When no card
covers that instant the perspective's fallback applies and the choice is
recorded on the estimate, so a summary can say how many charges were priced
on a fallback card. There is no "latest" default.

Model lookup is exact name or explicit alias. There is no prefix guessing:
an unknown model stays unpriced.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from importlib import resources
from pathlib import Path
from typing import Any

EFFECTIVE_AT_EXECUTION = "effective_at_execution"
FALLBACK_NEAREST_EARLIER = "fallback_nearest_earlier"
FALLBACK_NEAREST_LATER = "fallback_nearest_later"
PINNED = "pinned"
NONE = "none"


@dataclass(frozen=True)
class Prices:
    input: str
    cached_input: str  # cache-read (hit) price
    output: str
    cache_write_5m: str | None = None  # prompt-cache write prices (Anthropic); None = not priced separately
    cache_write_1h: str | None = None


@dataclass(frozen=True)
class RateCard:
    rate_card_id: str
    provider: str
    source: str  # list | contract
    currency: str
    effective_from: date
    effective_to: date | None
    models: dict[str, Prices]
    aliases: dict[str, str]
    note: str = ""

    def covers(self, at: date) -> bool:
        if at < self.effective_from:
            return False
        return self.effective_to is None or at <= self.effective_to

    def model_key(self, model: str | None) -> str | None:
        if not model:
            return None
        if model in self.models:
            return model
        alias = self.aliases.get(model)
        if alias in self.models:
            return alias
        return None

    def prices_for(self, model: str | None) -> tuple[str | None, Prices | None]:
        key = self.model_key(model)
        return key, (self.models[key] if key else None)


def _parse_card(data: dict[str, Any]) -> RateCard:
    models = {
        name: Prices(
            input=str(p["input"]),
            cached_input=str(p.get("cached_input", p["input"])),
            output=str(p["output"]),
            cache_write_5m=str(p["cache_write_5m"]) if p.get("cache_write_5m") is not None else None,
            cache_write_1h=str(p["cache_write_1h"]) if p.get("cache_write_1h") is not None else None,
        )
        for name, p in data["models"].items()
    }
    eff_to = data.get("effective_to")
    return RateCard(
        rate_card_id=data["rate_card_id"],
        provider=data["provider"],
        source=data.get("source", "list"),
        currency=data.get("currency", "USD"),
        effective_from=date.fromisoformat(data["effective_from"]),
        effective_to=date.fromisoformat(eff_to) if eff_to else None,
        models=models,
        aliases=dict(data.get("aliases", {})),
        note=str(data.get("note", "")),
    )


def load_card(path: Path) -> RateCard:
    with path.open("r", encoding="utf-8") as fh:
        return _parse_card(json.load(fh))


def load_builtin() -> list[RateCard]:
    cards: list[RateCard] = []
    pkg = resources.files("runpeek") / "rates"
    for entry in sorted(pkg.iterdir(), key=lambda e: e.name):
        if entry.name.endswith(".json"):
            cards.append(_parse_card(json.loads(entry.read_text(encoding="utf-8"))))
    return cards


class RateCardSet:
    def __init__(self, cards: list[RateCard]) -> None:
        self._cards = {c.rate_card_id: c for c in cards}

    @classmethod
    def builtin(cls, extra: list[RateCard] | None = None) -> RateCardSet:
        return cls(load_builtin() + list(extra or []))

    def get(self, rate_card_id: str) -> RateCard | None:
        return self._cards.get(rate_card_id)

    def ids(self) -> list[str]:
        return sorted(self._cards)

    def resolve(
        self,
        *,
        provider: str,
        source: str,
        at: datetime | None,
        fallback: str,
        pin: str | None,
    ) -> tuple[RateCard | None, str]:
        """Return (card, rate_resolution)."""
        if pin:
            card = self._cards.get(pin)
            return (card, PINNED) if card else (None, NONE)
        candidates = sorted(
            (c for c in self._cards.values() if c.provider == provider and c.source == source),
            key=lambda c: c.effective_from,
        )
        if not candidates:
            return None, NONE
        if at is None:
            # No execution time: only a fallback can apply.
            return _fallback(candidates, None, fallback)
        d = at.date()
        # Several cards may cover a date (an older open-ended card and a newer
        # one). The newest effective_from wins; older estimates keep their own
        # card id, so nothing already computed changes.
        covering = [c for c in candidates if c.covers(d)]
        if covering:
            return covering[-1], EFFECTIVE_AT_EXECUTION
        return _fallback(candidates, d, fallback)


def _fallback(candidates: list[RateCard], d: date | None, fallback: str) -> tuple[RateCard | None, str]:
    if fallback == "none":
        return None, NONE
    if d is None:
        return candidates[-1], FALLBACK_NEAREST_EARLIER
    earlier = [c for c in candidates if c.effective_from <= d]
    later = [c for c in candidates if c.effective_from > d]
    if fallback == "nearest_earlier":
        if earlier:
            return earlier[-1], FALLBACK_NEAREST_EARLIER
        return (later[0], FALLBACK_NEAREST_LATER) if later else (None, NONE)
    # nearest: compare distances
    best: tuple[int, RateCard, str] | None = None
    if earlier:
        c = earlier[-1]
        best = ((d - (c.effective_to or c.effective_from)).days, c, FALLBACK_NEAREST_EARLIER)
    if later:
        c = later[0]
        dist = (c.effective_from - d).days
        if best is None or dist < best[0]:
            best = (dist, c, FALLBACK_NEAREST_LATER)
    return (best[1], best[2]) if best else (None, NONE)
