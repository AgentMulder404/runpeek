"""Adapter registry. M1 ships one adapter."""

from __future__ import annotations

from typing import Any

from ..store import Emitter
from . import openai_chat

ADAPTERS = (openai_chat,)


def install_all(emit: Emitter) -> list[dict[str, Any]]:
    return [a.install(emit) for a in ADAPTERS]


def uninstall_all() -> None:
    for a in ADAPTERS:
        a.uninstall()
