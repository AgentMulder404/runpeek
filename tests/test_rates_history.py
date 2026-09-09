"""Historical estimate stability: adding a newer card never changes what an
older attempt resolves to, and newly verified models are not backdated."""

from __future__ import annotations

from datetime import datetime, timezone

from nemulai.money import tokens_cost_nanos
from nemulai.rates import RateCardSet


def _at(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def test_original_card_is_unchanged() -> None:
    card = RateCardSet.builtin().get("openai-list@2025-08-01")
    assert card is not None
    assert sorted(card.models) == [
        "gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano", "gpt-4o", "gpt-4o-mini",
        "gpt-5", "gpt-5-mini", "gpt-5-nano", "o3", "o4-mini",
    ]
    assert card.effective_to is None
    assert "gpt-6-astra" not in card.models


def test_august_attempt_still_resolves_to_original_card() -> None:
    cards = RateCardSet.builtin()
    card, res = cards.resolve(provider="openai", source="list", at=_at("2026-08-15T12:00:00"),
                              fallback="nearest_earlier", pin=None)
    assert card is not None and card.rate_card_id == "openai-list@2025-08-01"
    assert res == "effective_at_execution"
    _, prices = card.prices_for("gpt-4.1-mini")
    assert prices is not None
    assert tokens_cost_nanos(1000, prices.input) + tokens_cost_nanos(500, prices.output) == 1_200_000


def test_september_attempt_resolves_to_new_card_with_equal_price() -> None:
    cards = RateCardSet.builtin()
    card, res = cards.resolve(provider="openai", source="list", at=_at("2026-09-10T00:00:00"),
                              fallback="nearest_earlier", pin=None)
    assert card is not None and card.rate_card_id == "openai-list@2026-09-09"
    assert res == "effective_at_execution"
    old = cards.get("openai-list@2025-08-01")
    assert old is not None
    for m in old.models:
        assert card.models[m] == old.models[m], m  # verified equal on the retrieval date


def test_new_model_is_not_backdated() -> None:
    cards = RateCardSet.builtin()
    aug, _ = cards.resolve(provider="openai", source="list", at=_at("2026-08-15T00:00:00"),
                           fallback="nearest_earlier", pin=None)
    assert aug is not None and aug.prices_for("gpt-6-astra") == (None, None)
    sep, _ = cards.resolve(provider="openai", source="list", at=_at("2026-09-09T00:00:00"),
                           fallback="nearest_earlier", pin=None)
    assert sep is not None and sep.prices_for("gpt-6-astra")[1] is not None


def test_anthropic_card_before_verification_date_is_a_labelled_fallback() -> None:
    cards = RateCardSet.builtin()
    card, res = cards.resolve(provider="anthropic", source="list", at=_at("2026-08-01T00:00:00"),
                              fallback="nearest_earlier", pin=None)
    assert card is not None and card.rate_card_id == "anthropic-list@2026-09-09"
    assert res == "fallback_nearest_later"
    none, res2 = cards.resolve(provider="anthropic", source="list", at=_at("2026-08-01T00:00:00"),
                               fallback="none", pin=None)
    assert none is None and res2 == "none"
    _, prices = card.prices_for("claude-opus-5")
    assert prices is not None and prices.cache_write_5m == "6.25" and prices.cached_input == "0.50"
