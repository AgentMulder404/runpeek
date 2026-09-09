"""Normalised events every source adapter emits. No content fields exist here
by construction: an adapter that wants to persist a prompt has nowhere to put it."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TranscriptFile:
    path: Path
    session_id: str
    project_path: str | None
    parent_session_id: str | None = None

    @property
    def is_subagent(self) -> bool:
        return self.parent_session_id is not None


@dataclass(frozen=True)
class SessionInfo:
    session_id: str
    source_version: str | None
    project_path: str | None
    at: str | None


@dataclass(frozen=True)
class TurnStartEvent:
    turn_id: str
    at: str | None


@dataclass(frozen=True)
class TurnDurationEvent:
    duration_ms: int | None
    at: str | None


@dataclass(frozen=True)
class ToolUseEvent:
    tool_use_id: str
    at: str | None
    tool_name: str
    action_kind: str
    target: str | None
    # In-memory only: the normalised argument view that gets fingerprinted.
    # Never persisted. Adapters put only what identifies "the same action".
    fingerprint_input: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True)
class ToolResultEvent:
    tool_use_id: str
    at: str | None
    is_error: bool | None


@dataclass(frozen=True)
class UsageEvent:
    usage_id: str  # source's message id
    request_id: str | None
    model: str | None
    at: str | None
    input_tokens: int | None
    cache_write_5m_tokens: int | None
    cache_write_1h_tokens: int | None
    cache_read_tokens: int | None
    output_tokens: int | None
    web_search_requests: int | None
    web_fetch_requests: int | None
    usage_kind: str = "per_request"  # per_request | cumulative_snapshot
    provenance: str = "provider_reported"
    source_cost_nanos: int | None = None

    @property
    def has_usage(self) -> bool:
        return any(v is not None for v in (self.input_tokens, self.output_tokens, self.cache_read_tokens))


@dataclass(frozen=True)
class Unparseable:
    line_no: int
    reason: str


Event = (
    SessionInfo | TurnStartEvent | TurnDurationEvent | ToolUseEvent | ToolResultEvent | UsageEvent | Unparseable
)
