"""Attribution context.

A ContextVar holds the innermost ``Attribution``. ``job()`` pushes one and
restores the previous value on exit — including exit by exception — so nested
contexts and failures never leak attribution into unrelated calls.

ContextVars propagate across ``await`` and ``asyncio.create_task``. They do
NOT propagate into threads started by ``threading`` or executors; ``wrap()``
carries the current context into such a callable, and ``inject()`` /
``extract()`` carry it across processes or services as a plain dict.
"""

from __future__ import annotations

import contextvars
import functools
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, TypeVar

from .ids import new_id, now_iso

T = TypeVar("T")

ATTRIBUTED = "attributed"
JOB_ONLY = "job_only"
UNATTRIBUTED = "unattributed"


@dataclass(frozen=True)
class Attribution:
    customer: str | None
    job: str | None
    job_id: str | None
    parent_job_id: str | None
    span_id: str | None
    attributes: tuple[tuple[str, str], ...] = ()

    @property
    def state(self) -> str:
        return attribution_state(self.customer, self.job)


def attribution_state(customer: str | None, job: str | None) -> str:
    if customer:
        return ATTRIBUTED
    if job:
        return JOB_ONLY
    return UNATTRIBUTED


_current: contextvars.ContextVar[Attribution | None] = contextvars.ContextVar(
    "runpeek_attribution", default=None
)

# Set by bootstrap.install(); receives ("span_start" | "span_end", payload).
# Kept as a module-level indirection so this module never imports the store.
_emitter: Callable[[str, dict[str, Any]], Any] | None = None


def set_emitter(emit: Callable[[str, dict[str, Any]], Any] | None) -> None:
    global _emitter
    _emitter = emit


def _emit(kind: str, payload: dict[str, Any]) -> None:
    if _emitter is None:
        return
    try:
        _emitter(kind, payload)
    except Exception:  # never let telemetry break the application
        pass


def current() -> Attribution | None:
    return _current.get()


@contextmanager
def job(customer: str | None = None, job: str | None = None, **attributes: str) -> Iterator[Attribution]:
    """Attribute every observed call inside the block.

    A nested ``job()`` inherits ``customer`` (and ``job`` if not given), gets a
    fresh ``job_id`` and records ``parent_job_id``. Attributes merge, inner
    winning. The previous context is restored on exit, whatever the exit.
    """
    parent = _current.get()
    merged: dict[str, str] = dict(parent.attributes) if parent else {}
    merged.update({k: str(v) for k, v in attributes.items()})
    attr = Attribution(
        customer=customer if customer is not None else (parent.customer if parent else None),
        job=job if job is not None else (parent.job if parent else None),
        job_id=new_id("job"),
        parent_job_id=parent.job_id if parent else None,
        span_id=new_id("span"),
        attributes=tuple(sorted(merged.items())),
    )
    token = _current.set(attr)
    _emit(
        "span_start",
        {
            "span_id": attr.span_id,
            "parent_span_id": parent.span_id if parent else None,
            "kind": "job",
            "name": attr.job,
            "customer_id": attr.customer,
            "job_name": attr.job,
            "job_id": attr.job_id,
            "parent_job_id": attr.parent_job_id,
            "attributes": dict(attr.attributes),
            "started_at": now_iso(),
        },
    )
    status = "ok"
    try:
        yield attr
    except BaseException:
        status = "error"
        raise
    finally:
        _current.reset(token)
        _emit("span_end", {"span_id": attr.span_id, "ended_at": now_iso(), "status": status})


def inject() -> dict[str, str]:
    """Serialise the current attribution for another process or service."""
    attr = _current.get()
    if attr is None:
        return {}
    out: dict[str, str] = {}
    if attr.customer is not None:
        out["runpeek-customer"] = attr.customer
    if attr.job is not None:
        out["runpeek-job"] = attr.job
    if attr.job_id is not None:
        out["runpeek-job-id"] = attr.job_id
    for k, v in attr.attributes:
        out[f"runpeek-attr-{k}"] = v
    return out


@contextmanager
def extract(carrier: Mapping[str, str]) -> Iterator[Attribution | None]:
    """Adopt attribution from ``inject()`` output. No new span is created;
    the remote job_id is kept as parent so the chain stays visible."""
    if not carrier:
        yield _current.get()
        return
    def get(key: str) -> str | None:  # runpeek-* keys, with legacy nemulai-* accepted
        return carrier.get(f"runpeek-{key}", carrier.get(f"nemulai-{key}"))

    attrs = {k.split("-attr-", 1)[1]: v for k, v in carrier.items()
             if k.startswith("runpeek-attr-") or k.startswith("nemulai-attr-")}
    attr = Attribution(
        customer=get("customer"),
        job=get("job"),
        job_id=new_id("job"),
        parent_job_id=get("job-id"),
        span_id=None,
        attributes=tuple(sorted(attrs.items())),
    )
    token = _current.set(attr)
    try:
        yield attr
    finally:
        _current.reset(token)


def wrap(fn: Callable[..., T]) -> Callable[..., T]:
    """Return a callable that runs ``fn`` under a copy of the *current*
    context. Use it when handing work to a thread or executor."""
    ctx = contextvars.copy_context()

    @functools.wraps(fn)
    def runner(*args: Any, **kwargs: Any) -> T:
        return ctx.copy().run(fn, *args, **kwargs)

    return runner
