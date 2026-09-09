"""Coding-agent session observer.

A *source adapter* turns an agent's own session files into normalised events
(session, turn, action, usage). The ingestor persists allowlisted metadata
with checkpoints; the diagnostics run deterministically over what was stored.

Vocabulary (kept distinct on purpose):
  session   one coding-agent conversation (one transcript file)
  turn      one user prompt and everything the agent did in response
  action    one tool execution by the agent
  usage     one provider usage observation, when the source exposes it
"""

from __future__ import annotations

from .events import (
    Event,
    SessionInfo,
    ToolResultEvent,
    ToolUseEvent,
    TranscriptFile,
    TurnDurationEvent,
    TurnStartEvent,
    Unparseable,
    UsageEvent,
)

__all__ = [
    "Event", "SessionInfo", "ToolResultEvent", "ToolUseEvent", "TranscriptFile",
    "TurnDurationEvent", "TurnStartEvent", "Unparseable", "UsageEvent",
]
