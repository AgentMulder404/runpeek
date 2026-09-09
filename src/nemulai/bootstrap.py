"""Process bootstrap: open the store, install adapters, register shutdown.

Idempotent per process. Configuration is environment-driven so that
``nemulai run`` and a manual ``nemulai.install()`` behave identically.

  NEMULAI_DB                 store path (default ./.nemulai/nemulai.db)
  NEMULAI_RUN_ID             run id (default: minted)
  NEMULAI_COMMAND            command line recorded on the run
  NEMULAI_QUEUE_SIZE         bounded queue size (default 10000)
  NEMULAI_FLUSH_DEADLINE_S   shutdown flush deadline (default 5)
"""

from __future__ import annotations

import atexit
import os
import threading
from pathlib import Path
from typing import Any

from . import context, instrumentation
from .ids import new_id
from .store import SQLiteStore

_lock = threading.Lock()
_state: dict[str, Any] = {"store": None, "adapters": None}

DEFAULT_DB = Path(".nemulai") / "nemulai.db"


def install(
    db_path: str | Path | None = None,
    run_id: str | None = None,
    command: str | None = None,
    queue_size: int | None = None,
) -> dict[str, Any]:
    with _lock:
        if _state["store"] is not None:
            return {"installed": True, "already": True, "run_id": _state["store"].run_id,
                    "adapters": _state["adapters"]}
        path = Path(db_path or os.environ.get("NEMULAI_DB") or DEFAULT_DB)
        rid = run_id or os.environ.get("NEMULAI_RUN_ID") or new_id("run")
        cmd = command or os.environ.get("NEMULAI_COMMAND")
        qs = queue_size or int(os.environ.get("NEMULAI_QUEUE_SIZE", "10000"))
        store = SQLiteStore(path, rid, command=cmd, queue_size=qs)
        store.start()
        context.set_emitter(store.emit)
        adapters = instrumentation.install_all(store.emit)
        _state["store"] = store
        _state["adapters"] = adapters
        atexit.register(shutdown)
        return {"installed": True, "already": False, "run_id": rid, "db": str(path), "adapters": adapters}


def shutdown() -> dict[str, int] | None:
    with _lock:
        store: SQLiteStore | None = _state["store"]
        if store is None:
            return None
        deadline = float(os.environ.get("NEMULAI_FLUSH_DEADLINE_S", "5"))
        counters = store.close(deadline)
        context.set_emitter(None)
        instrumentation.uninstall_all()
        _state["store"] = None
        _state["adapters"] = None
        return counters


def current_store() -> SQLiteStore | None:
    return _state["store"]  # type: ignore[no-any-return]
