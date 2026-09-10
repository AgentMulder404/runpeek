from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from runpeek import context
from runpeek.context import current, extract, inject, job, wrap


def test_nested_inherits_customer_and_links_parent() -> None:
    assert current() is None
    with job(customer="acme", job="outer", region="eu") as outer:
        assert outer.state == "attributed"
        with job(job="inner", tier="gold") as inner:
            assert inner.customer == "acme"
            assert inner.job == "inner"
            assert inner.parent_job_id == outer.job_id
            assert inner.job_id != outer.job_id
            assert dict(inner.attributes) == {"region": "eu", "tier": "gold"}
        assert current() is outer
    assert current() is None


def test_restored_after_exception() -> None:
    with pytest.raises(ValueError):
        with job(customer="acme"):
            raise ValueError("boom")
    assert current() is None
    with job(customer="a") as a:
        with pytest.raises(RuntimeError):
            with job(customer="b"):
                raise RuntimeError
        assert current() is a


def test_states() -> None:
    with job(job="only"):
        assert current().state == "job_only"  # type: ignore[union-attr]
    with job(customer="c"):
        assert current().state == "attributed"  # type: ignore[union-attr]
    assert context.attribution_state(None, None) == "unattributed"


def test_async_tasks_inherit() -> None:
    async def main() -> str | None:
        with job(customer="acme"):
            return await asyncio.create_task(asyncio.sleep(0, result=None)) or (
                current().customer if current() else None
            )

    assert asyncio.run(main()) == "acme"


def test_threads_do_not_inherit_but_wrap_carries() -> None:
    seen: dict[str, str | None] = {}

    def probe(key: str) -> None:
        seen[key] = current().customer if current() else None

    with job(customer="acme"):
        t = threading.Thread(target=probe, args=("plain",))
        t.start()
        t.join()
        with ThreadPoolExecutor(1) as ex:
            ex.submit(probe, "pool").result()
            ex.submit(wrap(probe), "wrapped").result()
    assert seen == {"plain": None, "pool": None, "wrapped": "acme"}


def test_inject_extract_roundtrip() -> None:
    with job(customer="acme", job="j", k="v") as a:
        carrier = inject()
    assert carrier["runpeek-customer"] == "acme"
    with extract(carrier) as b:
        assert b is not None
        assert b.customer == "acme" and b.job == "j"
        assert b.parent_job_id == a.job_id
        assert dict(b.attributes) == {"k": "v"}
    assert current() is None
    with extract({}) as none:
        assert none is None


def test_emitter_failures_never_propagate() -> None:
    def bad(kind: str, payload: dict[str, object]) -> None:
        raise RuntimeError("telemetry down")

    context.set_emitter(bad)
    try:
        with job(customer="x"):
            pass
    finally:
        context.set_emitter(None)
