"""NemulAI — local-first observability and efficiency harness for AI workloads.

    import runpeek

    with runpeek.job(customer="acme", job="support_ticket"):
        client.chat.completions.create(...)   # observed, attributed, priced locally

M1 observes the OpenAI Python SDK's synchronous, non-streaming
``chat.completions.create`` and nothing else. See README.md for the support
table.
"""

from __future__ import annotations

__version__ = "0.1.0.dev0"

from .context import Attribution, current, extract, inject, job, wrap  # noqa: E402

__all__ = ["Attribution", "__version__", "current", "extract", "inject", "install", "job", "wrap"]


def install(**kwargs: object) -> dict[str, object]:
    """Manual bootstrap for processes not launched with ``runpeek run``."""
    from .bootstrap import install as _install

    return _install(**kwargs)  # type: ignore[arg-type]
