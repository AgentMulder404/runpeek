"""Terminal presentation helpers. No dependencies.

Colour is decoration only: every label carries its meaning as text. Colour is
off when NO_COLOR is set, when the stream is not a TTY, or when asked.
Anything that came from outside (paths, model names, labels) passes through
``sanitize`` before it reaches the terminal.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import textwrap
from datetime import datetime, timezone
from typing import IO

from .money import NANOS_PER_USD, format_usd

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]|\x9b[0-9;?]*[@-~]")
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\x80-\x9f]")

MIN_WIDTH = 40
MAX_WIDTH = 100


def sanitize(text: object) -> str:
    """Strip escape sequences and control characters from externally supplied text."""
    s = str(text)
    s = _ANSI.sub("", s)
    return _CTRL.sub("?", s)


class Term:
    def __init__(self, stream: IO[str] | None = None, *, color: bool | None = None, width: int | None = None) -> None:
        self.stream = stream or sys.stdout
        if color is None:
            isatty = bool(getattr(self.stream, "isatty", lambda: False)())
            color = isatty and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"
        self.color = bool(color)
        if width is None:
            env = os.environ.get("COLUMNS")
            try:
                width = int(env) if env else shutil.get_terminal_size((80, 24)).columns
            except ValueError:
                width = 80
        self.width = max(MIN_WIDTH, min(MAX_WIDTH, int(width)))

    # ---- colour (meaningless without the text label; never the only signal)

    def _c(self, code: str, s: str) -> str:
        return f"\x1b[{code}m{s}\x1b[0m" if self.color else s

    def green(self, s: str) -> str:
        return self._c("32", s)

    def amber(self, s: str) -> str:
        return self._c("33", s)

    def red(self, s: str) -> str:
        return self._c("31", s)

    def bold(self, s: str) -> str:
        return self._c("1", s)

    def dim(self, s: str) -> str:
        return self._c("2", s)

    # ---- layout

    def wrap(self, text: str, indent: int = 0, first_indent: int | None = None) -> list[str]:
        fi = indent if first_indent is None else first_indent
        return textwrap.wrap(text, width=self.width, initial_indent=" " * fi, subsequent_indent=" " * indent,
                             break_long_words=False, break_on_hyphens=False) or [" " * fi]

    def rule(self) -> str:
        return "─" * min(self.width, 72)


def usd_cents(nanos: int | None) -> str:
    """Two-decimal display that never turns a small positive amount into $0.00."""
    if nanos is None:
        return "—"
    if nanos == 0:
        return "$0.00"
    if 0 < nanos < NANOS_PER_USD // 200:  # under half a cent
        return "<$0.01"
    return f"${nanos / NANOS_PER_USD:,.2f}"


def usd_exact(nanos: int | None) -> str:
    return format_usd(nanos)


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def when(iso: str | None, now: datetime | None = None) -> str:
    """'Today 16:40' / 'Yesterday 09:12' / 'Sep 03 14:12' in local time."""
    d = parse_ts(iso)
    if d is None:
        return "—"
    now = now or datetime.now(timezone.utc)
    local = d.astimezone()
    today = now.astimezone().date()
    delta = (today - local.date()).days
    if delta == 0:
        return f"Today {local:%H:%M}"
    if delta == 1:
        return f"Yesterday {local:%H:%M}"
    return f"{local:%b %d %H:%M}"


def clock(iso: str | None) -> str:
    d = parse_ts(iso)
    return f"{d.astimezone():%H:%M}" if d else "--:--"


def duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    s = int(round(seconds))
    if s < 60:
        return f"{s} seconds"
    if s < 3600:
        return f"{s // 60} min {s % 60:02d} s"
    return f"{s // 3600} h {(s % 3600) // 60:02d} min"


def project_name(path: str | None) -> str:
    if not path:
        return "(unknown project)"
    return sanitize(os.path.basename(path.rstrip("/")) or path)
