"""Deterministic diagnostics over stored action metadata. No content, no
network, no model calls. Every finding is a *potential inefficiency* with
its evidence, counts, limitations and a suggestion that follows from them.

What counts as "a relevant change" is deliberately narrow and observable: an
Edit/Write action recorded between two events. Bash side effects (git
checkout, package installs) are not visible as changes, and external edits
are never visible — both are stated in each finding's limitations.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
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
    summary: str
    evidence: list[dict[str, Any]]
    counts: dict[str, Any]
    limitations: str
    suggestion: str
    first_at: str | None
    last_at: str | None

    @property
    def fingerprint(self) -> str:
        ids = [e["action_id"] for e in self.evidence]
        raw = f"{self.kind}|{self.session_id}|{ids[0] if ids else ''}|{ids[-1] if ids else ''}|{len(ids)}"
        return hashlib.sha256(raw.encode()).hexdigest()[:32]


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


# ------------------------------------------------------------------ detectors


def repeated_failing_actions(session_id: str, actions: list[Action]) -> list[Finding]:
    """Same normalised action failing ≥ N times with no Edit/Write between."""
    out: list[Finding] = []
    runs: dict[str, list[Action]] = {}
    for a in actions:
        if a.action_kind in _MUTATING_KINDS and a.is_error != 1:
            # any observed change resets every open run
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
    what = f"{first.tool_name}" + (f" on {first.target}" if first.target else "")
    return Finding(
        kind="repeated_failing_action",
        session_id=session_id,
        turn_id=first.turn_id,
        summary=f"{what} failed {len(run)} times in a row with no edit or write recorded in between",
        evidence=[_ev(a) for a in run],
        counts={"failures": len(run), "window_seconds": round(span, 1) if span is not None else None,
                "turns_spanned": len({a.turn_id for a in run})},
        limitations=("Only Edit/Write tool calls count as an observed change. Changes made through Bash "
                     "(git, package installs, generated files) or outside the agent are not visible here, so "
                     "some repeats may have followed a real change. Error content was not inspected."),
        suggestion=("Read the failure once and change something before the next attempt — or ask the agent to "
                    "stop and report the error instead of retrying the same command."),
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
        summary=f"{first.target} was read {len(run)} times with no edit to it recorded in between",
        evidence=[_ev(a) for a in run],
        counts={"reads": len(run), "distinct_ranges": distinct_ranges,
                "window_seconds": round(span, 1) if span is not None else None,
                "turns_spanned": len({a.turn_id for a in run})},
        limitations=("This shows the file was requested repeatedly; whether its contents were re-sent as model "
                     "input each time is not observable from the transcript, so no token or cost saving is "
                     "claimed. External modifications to the file are not visible."
                     + (" Different offsets/limits were used, so some reads may have targeted different parts."
                        if distinct_ranges > 1 else "")),
        suggestion=("If the same file keeps being needed, read it once with the narrowest useful range, or put "
                    "the relevant excerpt where the agent will retain it (e.g. CLAUDE.md or the task prompt)."),
        first_at=first.requested_at,
        last_at=last.requested_at,
    )


def retry_loops(session_id: str, actions: list[Action]) -> list[Finding]:
    """(a) ≥ N consecutive errors from one tool inside a window, no success of
    that tool between; (b) the same action executed ≥ M times inside a short
    window regardless of result. Silence is never evidence."""
    out: list[Finding] = []
    # (a) consecutive errors per tool
    err_runs: dict[str, list[Action]] = {}
    for a in actions:
        if a.is_error is None:
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
    # (b) tight repetition of one fingerprint
    by_fp: dict[str, list[Action]] = {}
    for a in actions:
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
        summary = f"{first.tool_name} returned {len(run)} errors in a row"
        suggestion = ("Check whether the tool itself is failing (service, permissions, environment) before "
                      "continuing; repeated identical errors rarely resolve on their own.")
    else:
        summary = (f"the same {first.tool_name} action" + (f" ({first.target})" if first.target else "")
                   + f" ran {len(run)} times within {round(span or 0)} s")
        suggestion = ("Look at what changed between runs; if nothing did, the loop is not converging and a "
                      "different approach or a human decision is needed.")
    return Finding(
        kind="retry_loop",
        session_id=session_id,
        turn_id=first.turn_id,
        summary=summary,
        evidence=[_ev(a) for a in run],
        counts={"actions": len(run), "errors": sum(1 for a in run if a.is_error == 1), "mode": mode,
                "window_seconds": round(span, 1) if span is not None else None},
        limitations=("Based only on recorded tool calls and their timestamps. Gaps, permission prompts and "
                     "user thinking time are never treated as evidence. Error content was not inspected; the "
                     "tool may have been failing for different reasons each time."),
        suggestion=suggestion,
        first_at=first.requested_at,
        last_at=last.requested_at,
    )


# -------------------------------------------------------------------- driver


def analyse_session(conn: sqlite3.Connection, session_id: str) -> int:
    """Run all detectors and upsert findings. Returns new findings count."""
    actions = load_actions(conn, session_id)
    if not actions:
        return 0
    findings = (
        repeated_failing_actions(session_id, actions)
        + repeated_reads(session_id, actions)
        + retry_loops(session_id, actions)
    )
    new = 0
    for f in findings:
        usage = _usage_for(conn, session_id, f.first_at, f.last_at)
        counts = dict(f.counts)
        counts["usage_in_window"] = usage
        cur = conn.execute(
            "INSERT OR IGNORE INTO agent_findings (finding_id, fingerprint, session_id, turn_id, kind, severity,"
            " summary, evidence, counts, limitations, suggestion, first_at, last_at, created_at)"
            " VALUES (?,?,?,?,?,'potential_inefficiency',?,?,?,?,?,?,?,?)",
            (new_id("fnd"), f.fingerprint, f.session_id, f.turn_id, f.kind, f.summary,
             json.dumps(f.evidence, separators=(",", ":")), json.dumps(counts, separators=(",", ":")),
             f.limitations, f.suggestion, f.first_at, f.last_at, now_iso()),
        )
        new += 1 if cur.rowcount > 0 else 0
    return new


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
            "note": "provider-reported usage for API requests timestamped inside the finding's window"}
