"""Injected by ``nemulai run`` via PYTHONPATH.

Runs at interpreter start (``site`` processing), before the application
imports its provider client, so class patching happens first.

Two rules:
1. Fail open. Any failure prints one stderr line and the application runs
   uninstrumented.
2. Chain. If another ``sitecustomize`` exists further along ``sys.path`` it is
   executed too, so adding NemulAI never silently disables someone else's
   startup hook.
"""

import importlib.machinery
import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))

if os.environ.get("NEMULAI_ENABLED") == "1":
    try:
        import nemulai

        nemulai.install()
    except Exception as _exc:  # pragma: no cover - exercised via subprocess tests
        sys.stderr.write(f"nemulai: bootstrap failed, application runs uninstrumented: {_exc!r}\n")


def _chain_existing_sitecustomize() -> None:
    others = [p for p in sys.path if os.path.abspath(p or ".") != _HERE]
    spec = importlib.machinery.PathFinder.find_spec("sitecustomize", others)
    if spec is None or spec.origin is None or spec.loader is None:
        return
    if os.path.abspath(spec.origin) == os.path.abspath(__file__):
        return
    try:
        mod = importlib.util.module_from_spec(spec)
        sys.modules["_nemulai_chained_sitecustomize"] = mod
        spec.loader.exec_module(mod)
    except Exception as exc:
        sys.stderr.write(f"nemulai: chained sitecustomize at {spec.origin} failed: {exc!r}\n")


_chain_existing_sitecustomize()
