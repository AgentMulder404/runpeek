"""OpenAI Python SDK — ``chat.completions.create``, synchronous, non-streaming.

Tested against ``openai`` 2.44.0. What this adapter relies on, with the
source locations checked on 2026-09-09:

* ``openai.resources.chat.completions.Completions.create`` is the method an
  application calls; the SDK's retry loop runs below it in
  ``_base_client.request()`` (``_base_client.py:1017``). One invocation here
  is therefore one *operation* and exactly one *observable attempt* — the
  last one. Earlier internal retries are not visible from this layer.
* The parsed response carries ``id`` (provider object id) and
  ``_request_id`` (the ``x-request-id`` header; ``_models.py:133-141``).
* ``usage.prompt_tokens_details.cached_tokens`` is a breakdown of
  ``prompt_tokens`` and ``completion_tokens_details.reasoning_tokens`` a
  breakdown of ``completion_tokens`` (``types/completion_usage.py``);
  ``total_tokens = prompt + completion``. They are subsets and are never
  charged in addition to the totals they belong to.
* ``with_raw_response.create`` wraps the *bound* ``create`` when the
  ``cached_property`` is first accessed (``completions.py:72,3187-3195``).
  It is covered only if the class was patched before that first access.

Streaming (``stream=True``) is recorded as an operation with an attempt whose
``observed_status`` is ``unsupported_stream`` and no usage. The stream itself is
returned untouched. Async ``AsyncCompletions`` is not patched in M1.

Contract: the original method is invoked exactly once; its return value or
exception is passed through unchanged; every hook failure is swallowed and
counted.
"""

from __future__ import annotations

import functools
from typing import Any

from ..context import current
from ..ids import (
    HARNESS_OPERATION_ID,
    HTTP_REQUEST_ID,
    PROVIDER_OBJECT_ID,
    monotonic_ms,
    new_id,
    now_iso,
)
from ..store import Emitter

SURFACE = "openai.chat.completions.create"
PROVIDER = "openai"
SOURCE = "harness.openai"
TESTED_SDK = "openai==2.44.0"

# Test-only fault injection: "before" | "after" | None.
_FAULT: str | None = None

_emit: Emitter | None = None


class _Pre:
    __slots__ = ("operation_id", "attempt_id", "t0", "stream", "model")

    def __init__(self, operation_id: str, attempt_id: str, t0: float, stream: bool, model: str | None) -> None:
        self.operation_id = operation_id
        self.attempt_id = attempt_id
        self.t0 = t0
        self.stream = stream
        self.model = model


def _health(kind: str, detail: dict[str, Any] | None = None) -> None:
    if _emit is None:
        return
    try:
        _emit("health", {"kind": kind, "detail": detail, "at": now_iso()})
    except Exception:
        pass


def _before(kwargs: dict[str, Any]) -> _Pre | None:
    try:
        if _FAULT == "before":
            raise RuntimeError("injected hook failure (before)")
        if _emit is None:
            return None
        attr = current()
        stream = bool(kwargs.get("stream"))
        model = kwargs.get("model")
        pre = _Pre(new_id("op"), new_id("att"), monotonic_ms(), stream, model if isinstance(model, str) else None)
        started = now_iso()
        _emit(
            "operation",
            {
                "operation_id": pre.operation_id,
                "span_id": attr.span_id if attr else None,
                "surface": SURFACE,
                "supported": not stream,
                "started_at": started,
                "customer_id": attr.customer if attr else None,
                "job_name": attr.job if attr else None,
                "job_id": attr.job_id if attr else None,
                "attribution_state": attr.state if attr else "unattributed",
            },
        )
        _emit(
            "attempt_start",
            {
                "attempt_id": pre.attempt_id,
                "operation_id": pre.operation_id,
                "visibility": "last_only",
                "provider": PROVIDER,
                "model_requested": pre.model,
                "started_at": started,
            },
        )
        _emit(
            "identifier",
            {
                "id_kind": HARNESS_OPERATION_ID, "namespace": "runpeek", "value": pre.operation_id,
                "subject_kind": "operation", "subject_id": pre.operation_id, "source": SOURCE,
            },
        )
        if stream:
            _health("unsupported_surface", {"surface": SURFACE, "reason": "stream=True (M1 supports non-streaming)"})
        return pre
    except Exception as exc:
        _health("hook_failure_before", {"error": repr(exc)})
        return None


def _unwrap(result: Any) -> Any:
    """Raw-response wrappers expose ``parse()``; parsed models expose ``usage``."""
    if hasattr(result, "usage"):
        return result
    parse = getattr(result, "parse", None)
    if callable(parse) and hasattr(result, "http_response"):
        try:
            return parse()
        except Exception:
            return result
    return result


def _after_success(pre: _Pre | None, result: Any) -> None:
    try:
        if _FAULT == "after":
            raise RuntimeError("injected hook failure (after)")
        if _emit is None or pre is None:
            return
        ended = now_iso()
        latency = monotonic_ms() - pre.t0
        base = {
            "attempt_id": pre.attempt_id, "operation_id": pre.operation_id, "visibility": "last_only",
            "provider": PROVIDER, "model_requested": pre.model, "ended_at": ended, "latency_ms": latency,
        }
        if pre.stream:
            _emit("attempt_end", {**base, "observed_status": "unsupported_stream"})
            return
        parsed = _unwrap(result)
        usage = getattr(parsed, "usage", None)
        model_served = getattr(parsed, "model", None)
        obj_id = getattr(parsed, "id", None)
        req_id = getattr(parsed, "_request_id", None)
        _emit("attempt_end", {**base, "observed_status": "completed", "model_served": model_served,
                              "http_status": 200})
        obs: dict[str, Any] = {
            "observation_id": new_id("obs"), "attempt_id": pre.attempt_id, "source": SOURCE,
            "collected_at": ended, "model_served": model_served, "http_status": 200,
            "input_tokens": None, "cached_input_tokens": None, "output_tokens": None,
            "reasoning_tokens": None, "usage_source": "missing",
        }
        if usage is not None:
            inp = getattr(usage, "prompt_tokens", None)
            out = getattr(usage, "completion_tokens", None)
            ptd = getattr(usage, "prompt_tokens_details", None)
            ctd = getattr(usage, "completion_tokens_details", None)
            obs.update(
                input_tokens=inp if isinstance(inp, int) else None,
                output_tokens=out if isinstance(out, int) else None,
                cached_input_tokens=getattr(ptd, "cached_tokens", None) if ptd is not None else None,
                reasoning_tokens=getattr(ctd, "reasoning_tokens", None) if ctd is not None else None,
            )
            if obs["input_tokens"] is not None or obs["output_tokens"] is not None:
                obs["usage_source"] = "exact"
        _emit("observation", obs)
        if isinstance(obj_id, str) and obj_id:
            _emit("identifier", {"id_kind": PROVIDER_OBJECT_ID, "namespace": PROVIDER, "value": obj_id,
                                 "subject_kind": "attempt", "subject_id": pre.attempt_id, "source": SOURCE})
        if isinstance(req_id, str) and req_id:
            _emit("identifier", {"id_kind": HTTP_REQUEST_ID, "namespace": f"{PROVIDER}.x-request-id",
                                 "value": req_id, "subject_kind": "attempt", "subject_id": pre.attempt_id,
                                 "source": SOURCE})
    except Exception as exc:
        _health("hook_failure_after", {"error": repr(exc)})


def _after_error(pre: _Pre | None, exc: BaseException) -> None:
    try:
        if _emit is None or pre is None:
            return
        status = getattr(exc, "status_code", None)
        req_id = getattr(exc, "request_id", None)
        _emit(
            "attempt_end",
            {
                "attempt_id": pre.attempt_id, "operation_id": pre.operation_id, "visibility": "last_only",
                "provider": PROVIDER, "model_requested": pre.model, "ended_at": now_iso(),
                "latency_ms": monotonic_ms() - pre.t0, "observed_status": "provider_error",
                "error_class": type(exc).__name__, "http_status": status if isinstance(status, int) else None,
            },
        )
        if isinstance(req_id, str) and req_id:
            _emit("identifier", {"id_kind": HTTP_REQUEST_ID, "namespace": f"{PROVIDER}.x-request-id",
                                 "value": req_id, "subject_kind": "attempt", "subject_id": pre.attempt_id,
                                 "source": SOURCE})
    except Exception as hook_exc:
        _health("hook_failure_after", {"error": repr(hook_exc)})


def _wrap(original: Any) -> Any:
    @functools.wraps(original)
    def create(self: Any, *args: Any, **kwargs: Any) -> Any:
        pre = _before(kwargs)
        try:
            result = original(self, *args, **kwargs)  # exactly once
        except BaseException as exc:
            _after_error(pre, exc)
            raise
        _after_success(pre, result)
        return result

    create.__runpeek_wrapped__ = True  # type: ignore[attr-defined]
    create.__runpeek_original__ = original  # type: ignore[attr-defined]
    return create


def install(emit: Emitter) -> dict[str, Any]:
    """Patch the class method. Idempotent: a second call is a no-op."""
    global _emit
    _emit = emit
    try:
        from openai.resources.chat.completions import Completions
    except Exception as exc:  # openai not installed or import failed
        _health("adapter_failed", {"adapter": SURFACE, "error": repr(exc)})
        return {"adapter": SURFACE, "installed": False, "reason": repr(exc)}
    fn = Completions.__dict__.get("create")
    if fn is not None and getattr(fn, "__runpeek_wrapped__", False):
        return {"adapter": SURFACE, "installed": True, "already": True}
    Completions.create = _wrap(Completions.create)  # type: ignore[method-assign]
    _health("adapter_installed", {"adapter": SURFACE, "sdk": _sdk_version()})
    return {"adapter": SURFACE, "installed": True, "already": False}


def uninstall() -> None:
    global _emit
    try:
        from openai.resources.chat.completions import Completions
    except Exception:
        return
    fn = Completions.__dict__.get("create")
    orig = getattr(fn, "__runpeek_original__", None)
    if orig is not None:
        Completions.create = orig  # type: ignore[method-assign]
    _emit = None


def _sdk_version() -> str | None:
    try:
        import openai

        return str(openai.__version__)
    except Exception:
        return None
