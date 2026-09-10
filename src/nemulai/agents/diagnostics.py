"""Deterministic diagnostics over stored action metadata. No content, no
network, no model calls. Every item is a *potential inefficiency* with what
was observed, its evidence, a supported next step and the limitation needed to
read it correctly.

"An observed change" is deliberately narrow: an Edit/Write tool call recorded
between two events. Changes made through commands (git, installs, generated
files) or outside the agent are not visible, and every item says so.

Categories do not overlap: each action contributes to at most one item.
  repeated_failing_action  one identical action failing repeatedly
  repeated_read            one file read repeatedly without an observed edit
  retry_loop               errors across *different* inputs of one tool, or a
                           tight loop of one *succeeding* action
A finding is one row per (kind, session, first evidence action). When the
same run grows, the row is updated in place rather than duplicated.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..ids import new_id, now_iso

REPEATED_FAILURE_MIN = 3
REPEATED_READ_MIN = 3
RETRY_LOOP_MIN_ERRORS = 4
RETRY_LOOP_WINDOW_S = 600
TIGHT_LOOP_MIN = 5
TIGHT_LOOP_WINDOW_S = 180

_MUTATING_KINDS = {"edit", "write"}

TITLES = {
    "repeated_failing_action": "REPEATED FAILURE",
    "repeated_read": "REPEATED READ",
    "retry_loop:consecutive_errors": "REPEATED TOOL ERRORS",
    "retry_loop:tight_repetition": "TIGHT LOOP",
}


def title_for(kind: str, counts: dict[str, Any]) -> str:
    mode = counts.get("mode")
    return TITLES.get(f"{kind}:{mode}") or TITLES.get(kind) or kind.replace("_", " ").upper()


@dataclass
class Action:
    action_id: str
    turn_id: str | None
    sequence: int
    tool_name: str
    action_kind: str
    target: str | None
    fingerprint: str
    requested_at: str | None
    completed_at: str | None
    is_error: int | None

    @property
    def t(self) -> datetime | None:
        return _ts(self.requested_at)


@dataclass
class Finding:
    kind: str
    session_id: str
    turn_id: str | None
    summary: str  # one plain sentence: what was observed
    evidence: list[dict[str, Any]]
    counts: dict[str, Any]
    limitations: str
    suggestion: str
    first_at: str | None
    last_at: str | None

    @property
    def group_key(self) -> str:
        first = self.evidence[0]["action_id"] if self.evidence else ""
        return f"{self.kind}|{self.session_id}|{first}"

    @property
    def fingerprint(self) -> str:
        ids = [e["action_id"] for e in self.evidence]
        raw = f"{self.kind}|{self.session_id}|{ids[0] if ids else ''}|{ids[-1] if ids else ''}|{len(ids)}"
        return hashlib.sha256(raw.encode()).hexdigest()[:32]


@dataclass
class Delta:
    new: list[str] = field(default_factory=list)  # finding ids
    updated: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.new) + len(self.updated)


def _ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _span_s(a: Action, b: Action) -> float | None:
    ta, tb = a.t, b.t
    return (tb - ta).total_seconds() if ta and tb else None


def _window(span: float | None) -> str:
    if span is None:
        return "an unknown time span"
    s = int(round(span))
    if s < 90:
        return f"{s} seconds"
    if s < 3600:
        return f"{round(s / 60)} minutes"
    return f"{s / 3600:.1f} hours"


def load_actions(conn: sqlite3.Connection, session_id: str) -> list[Action]:
    rows = conn.execute(
        "SELECT action_id, turn_id, sequence, tool_name, action_kind, target, fingerprint, requested_at, completed_at,"
        " is_error FROM agent_actions WHERE session_id = ? ORDER BY sequence",
        (session_id,),
    ).fetchall()
    return [Action(*r) for r in rows]


def _ev(a: Action) -> dict[str, Any]:
    return {"action_id": a.action_id, "at": a.requested_at, "tool": a.tool_name, "target": a.target,
            "error": bool(a.is_error) if a.is_error is not None else None}


def _what(a: Action) -> str:
    """A safe noun phrase for an action: tool + allowlisted target only."""
    if a.action_kind == "bash":
        return f"A {a.target} command" if a.target else "A command"
    if a.target:
        return f"{a.tool_name} on {a.target}"
    return f"A {a.tool_name} call"


# ------------------------------------------------------------------ detectors


def repeated_failing_actions(session_id: str, actions: list[Action]) -> list[Finding]:
    """Same normalised action failing ≥ N times with no Edit/Write between."""
    out: list[Finding] = []
    runs: dict[str, list[Action]] = {}
    for a in actions:
        if a.action_kind in _MUTATING_KINDS and a.is_error != 1:
            for fp, run in list(runs.items()):
                if len(run) >= REPEATED_FAILURE_MIN:
                    out.append(_failure_finding(session_id, run))
                runs[fp] = []
            continue
        if a.is_error == 1:
            runs.setdefault(a.fingerprint, []).append(a)
        elif a.is_error == 0 and a.fingerprint in runs:
            run = runs.pop(a.fingerprint)
            if len(run) >= REPEATED_FAILURE_MIN:
                out.append(_failure_finding(session_id, run))
    for run in runs.values():
        if len(run) >= REPEATED_FAILURE_MIN:
            out.append(_failure_finding(session_id, run))
    return out


def _failure_finding(session_id: str, run: list[Action]) -> Finding:
    first, last = run[0], run[-1]
    span = _span_s(first, last)
    return Finding(
        kind="repeated_failing_action",
        session_id=session_id,
        turn_id=first.turn_id,
        summary=f"{_what(first)} failed {len(run)} times consecutively over {_window(span)}.",
        evidence=[_ev(a) for a in run],
        counts={"failures": len(run), "window_seconds": round(span, 1) if span is not None else None,
                "turns_spanned": len({a.turn_id for a in run})},
        limitations=("No edit or write was observed between the failures. Changes made through commands "
                     "(git, installs, generated files) or outside the agent are not visible, so a change may "
                     "have happened. The error output was not inspected."),
        suggestion="Check the error before repeating the command.",
        first_at=first.requested_at,
        last_at=last.requested_at,
    )


def repeated_reads(session_id: str, actions: list[Action]) -> list[Finding]:
    """Same file read ≥ N times with no Edit/Write to that file between reads."""
    out: list[Finding] = []
    reads: dict[str, list[Action]] = {}
    for a in actions:
        if a.action_kind in _MUTATING_KINDS and a.target:
            run = reads.pop(a.target, None)
            if run and len(run) >= REPEATED_READ_MIN:
                out.append(_read_finding(session_id, run))
            continue
        if a.action_kind == "read" and a.target and a.is_error != 1:
            reads.setdefault(a.target, []).append(a)
    for run in reads.values():
        if len(run) >= REPEATED_READ_MIN:
            out.append(_read_finding(session_id, run))
    return out


def _read_finding(session_id: str, run: list[Action]) -> Finding:
    first, last = run[0], run[-1]
    span = _span_s(first, last)
    distinct_ranges = len({a.fingerprint for a in run})
    return Finding(
        kind="repeated_read",
        session_id=session_id,
        turn_id=first.turn_id,
        summary=f"{first.target} was read {len(run)} times in {_window(span)}.",
        evidence=[_ev(a) for a in run],
        counts={"reads": len(run), "distinct_ranges": distinct_ranges,
                "window_seconds": round(span, 1) if span is not None else None,
                "turns_spanned": len({a.turn_id for a in run})},
        limitations=("No edit to that file was observed between reads. Repeated billing cannot be determined: "
                     "whether the contents were re-sent as model input each time is not visible in the "
                     "transcript. Changes made outside the agent are not visible."
                     + (" Different offsets or limits were used, so some reads may have covered different parts."
                        if distinct_ranges > 1 else "")),
        suggestion=("If the file is needed repeatedly, read it once with the narrowest useful range, or keep the "
                    "relevant excerpt in the task context."),
        first_at=first.requested_at,
        last_at=last.requested_at,
    )


def retry_loops(session_id: str, actions: list[Action], exclude: set[str] | None = None) -> list[Finding]:
    """(a) ≥ N consecutive errors from one tool across different inputs inside a
    window; (b) the same succeeding action executed ≥ M times inside a short
    window. Actions already covered by a repeated_failing_action item are
    excluded (``exclude``) so categories never overlap. Silence is never evidence."""
    out: list[Finding] = []
    exclude = exclude or set()
    err_runs: dict[str, list[Action]] = {}
    for a in actions:
        if a.is_error is None or a.action_id in exclude:
            continue
        if a.is_error == 1:
            run = err_runs.setdefault(a.tool_name, [])
            if run and (_span_s(run[-1], a) or 0) > RETRY_LOOP_WINDOW_S:
                if len(run) >= RETRY_LOOP_MIN_ERRORS and not _single_action(run):
                    out.append(_retry_finding(session_id, run, "consecutive_errors"))
                run.clear()
            run.append(a)
        else:
            done = err_runs.pop(a.tool_name, None)
            if done and len(done) >= RETRY_LOOP_MIN_ERRORS and not _single_action(done):
                out.append(_retry_finding(session_id, done, "consecutive_errors"))
    for run in err_runs.values():
        if len(run) >= RETRY_LOOP_MIN_ERRORS and not _single_action(run):
            out.append(_retry_finding(session_id, run, "consecutive_errors"))
    by_fp: dict[str, list[Action]] = {}
    for a in actions:
        if a.is_error != 1 and a.action_id not in exclude:  # failing repeats are repeated_failing_action
            by_fp.setdefault(a.fingerprint, []).append(a)
    seen: set[str] = set()
    for fp, group in by_fp.items():
        if len(group) < TIGHT_LOOP_MIN:
            continue
        i = 0
        while i < len(group):
            j = i
            while j + 1 < len(group) and (_span_s(group[i], group[j + 1]) or 1e9) <= TIGHT_LOOP_WINDOW_S:
                j += 1
            if j - i + 1 >= TIGHT_LOOP_MIN:
                window = group[i : j + 1]
                key = f"{fp}:{window[0].action_id}"
                if key not in seen:
                    seen.add(key)
                    out.append(_retry_finding(session_id, window, "tight_repetition"))
                i = j + 1
            else:
                i += 1
    return out


def _single_action(run: list[Action]) -> bool:
    return len({a.fingerprint for a in run}) == 1


def _retry_finding(session_id: str, run: list[Action], mode: str) -> Finding:
    first, last = run[0], run[-1]
    span = _span_s(first, last)
    if mode == "consecutive_errors":
        summary = (f"{first.tool_name} returned {len(run)} errors in a row across different inputs"
                   f" over {_window(span)}.")
        suggestion = "Check whether the tool or the service behind it is failing before continuing."
    else:
        summary = f"{_what(first)} ran {len(run)} times within {_window(span)}."
        suggestion = "Confirm something changed between runs; if not, the loop is not converging."
    return Finding(
        kind="retry_loop",
        session_id=session_id,
        turn_id=first.turn_id,
        summary=summary,
        evidence=[_ev(a) for a in run],
        counts={"actions": len(run), "errors": sum(1 for a in run if a.is_error == 1), "mode": mode,
                "window_seconds": round(span, 1) if span is not None else None},
        limitations=("Based only on recorded tool calls and their timestamps. Gaps, permission prompts and "
                     "thinking time are never treated as evidence. Error output was not inspected; the causes "
                     "may have differed each time."),
        suggestion=suggestion,
        first_at=first.requested_at,
        last_at=last.requested_at,
    )


# -------------------------------------------------------------------- driver


def analyse_session(conn: sqlite3.Connection, session_id: str) -> Delta:
    """Run all detectors; insert new items, update grown ones in place."""
    delta = Delta()
    actions = load_actions(conn, session_id)
    if not actions:
        return delta
    failures = repeated_failing_actions(session_id, actions)
    covered = {e["action_id"] for f in failures for e in f.evidence}
    findings = failures + repeated_reads(session_id, actions) + retry_loops(session_id, actions, covered)
    for f in findings:
        usage = _usage_for(conn, session_id, f.first_at, f.last_at)
        counts = dict(f.counts)
        counts["usage_in_window"] = usage
        existing = conn.execute(
            "SELECT finding_id, fingerprint FROM agent_findings WHERE group_key = ?", (f.group_key,)
        ).fetchone()
        if existing is None:
            fid = new_id("fnd")
            conn.execute(
                "INSERT INTO agent_findings (finding_id, fingerprint, group_key, session_id, turn_id, kind, severity,"
                " summary, evidence, counts, limitations, suggestion, first_at, last_at, created_at)"
                " VALUES (?,?,?,?,?,?,'potential_inefficiency',?,?,?,?,?,?,?,?)",
                (fid, f.fingerprint, f.group_key, f.session_id, f.turn_id, f.kind, f.summary,
                 json.dumps(f.evidence, separators=(",", ":")), json.dumps(counts, separators=(",", ":")),
                 f.limitations, f.suggestion, f.first_at, f.last_at, now_iso()),
            )
            delta.new.append(fid)
        elif existing["fingerprint"] != f.fingerprint:
            conn.execute(
                "UPDATE agent_findings SET fingerprint = ?, summary = ?, evidence = ?, counts = ?, limitations = ?,"
                " suggestion = ?, last_at = ?, updated_at = ? WHERE finding_id = ?",
                (f.fingerprint, f.summary, json.dumps(f.evidence, separators=(",", ":")),
                 json.dumps(counts, separators=(",", ":")), f.limitations, f.suggestion, f.last_at, now_iso(),
                 existing["finding_id"]),
            )
            delta.updated.append(str(existing["finding_id"]))
    return delta


def _usage_for(conn: sqlite3.Connection, session_id: str, first_at: str | None, last_at: str | None) -> dict[str, Any]:
    if not first_at or not last_at:
        return {"available": False}
    row = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(input_tokens),0) i, COALESCE(SUM(cache_read_tokens),0) cr,"
        " COALESCE(SUM(COALESCE(cache_write_5m_tokens,0)+COALESCE(cache_write_1h_tokens,0)),0) cw,"
        " COALESCE(SUM(output_tokens),0) o FROM agent_usage WHERE session_id = ? AND at >= ? AND at <= ?",
        (session_id, first_at, last_at),
    ).fetchone()
    return {"available": True, "requests": row["n"], "input_tokens": row["i"], "cache_read_tokens": row["cr"],
            "cache_write_tokens": row["cw"], "output_tokens": row["o"],
            "note": "usage reported in Claude Code transcripts for model calls timestamped inside the window"}
