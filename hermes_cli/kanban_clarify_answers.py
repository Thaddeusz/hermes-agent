"""Kanban answer capture — fold operator answers back into a parked triage task.

Companion module to ``hermes_cli/triage_clarify.py`` (the question generator
that parks a task in ``status=awaiting_clarification``). When the operator
hands back answers — typed at the CLI, piped through a file, or pasted into
stdin as JSON — this module is the thin front-end that:

  1. Parses the user input into the canonical ``[{id, answer}, ...]`` shape
     the data layer expects.
  2. Calls :func:`hermes_cli.kanban_db.fold_clarification_answers` to write
     the answers back, fold them into the task body, and flip the status
     to ``triage`` so the auto-decomposer picks the card up on its next tick.

This module is the CLI's adapter — keep the validation + parsing logic here
so the chat-webhook half (a future card) can reuse the same code path. The
DB layer (in ``kanban_db``) stays oblivious to how the answers were captured;
it only sees a list of dicts.

Design notes
------------

* Mirrors the shape of ``hermes_cli/kanban_specify.py``: no network calls,
  no aux LLM, just argument plumbing + a thin DB wrapper. The fold happens
  inside ``write_txn`` so a parse failure or a wrong-state task never
  leaves the row half-written.

* Three input modes are supported and may be mixed in a single invocation:
    - ``--q <id>=<text>`` (repeatable) for ad-hoc CLI use.
    - ``--answer-file <path>`` pointing at a JSON array of
      ``[{id, answer}, ...]`` (the typical "paste into a file" path).
    - stdin JSON (``--stdin``) for the same payload piped through the
      terminal. ``--stdin`` is explicit so the user has to opt in (we
      don't want to silently consume stdin from a non-interactive shell).

* ``submit_clarification_answers`` is the public entry point. It accepts
  the already-parsed list and is what the future webhook handler should
  call — the CLI argparse layer is responsible for parsing, this layer
  is responsible for validation + DB write + comment.

* Errors are split into :class:`ClarifyAnswerParseError` (user error —
  bad input, missing fields) and the underlying DB ``ValueError`` /
  ``None`` return so the CLI can render distinct messages.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from typing import Iterable, List, Optional, Sequence, TextIO

from hermes_cli import kanban_db as kb

logger = logging.getLogger(__name__)


class ClarifyAnswerParseError(ValueError):
    """Raised when the operator's input cannot be parsed into a valid
    ``[{id, answer}, ...]`` list.

    This is a user-facing error class — the CLI layer should render
    ``str(exc)`` to stderr with exit code 2 and skip the DB write.
    Distinct from the DB-layer ``ValueError`` raised by
    :func:`kanban_db.fold_clarification_answers` for the same shape
    failure (defense in depth: the CLI validates first, the DB
    validates again).
    """


# ``--q`` flag uses the literal ``=`` separator so spaces are easy
# to express on a shell. Reject embedded newlines (which would let a
# CLI user smuggle multi-answer payloads into a single flag) so the
# validation surface stays predictable.
_Q_FLAG_RE = re.compile(r"^([^=\s][^=]*)=(.*)$", re.DOTALL)


def _parse_q_flag(arg: str, *, index: int = 0) -> dict:
    """Parse a single ``--q <id>=<answer>`` flag value into ``{id, answer}``.

    The id side may contain any non-whitespace character except ``=``;
    the answer side is everything after the first ``=`` (so an empty
    answer is allowed but a missing ``=`` is rejected — see the regex).

    Raises :class:`ClarifyAnswerParseError` on bad input. The ``index``
    argument is purely for the error message so a typo on the third
    ``--q`` flag tells the operator which flag was malformed.
    """
    if not isinstance(arg, str):
        raise ClarifyAnswerParseError(
            f"--q[{index}]: expected a string, got {type(arg).__name__}"
        )
    m = _Q_FLAG_RE.match(arg)
    if not m:
        raise ClarifyAnswerParseError(
            f"--q[{index}]: expected '<id>=<answer>', got {arg!r}"
        )
    qid, ans = m.group(1), m.group(2)
    qid = qid.strip()
    if not qid:
        raise ClarifyAnswerParseError(
            f"--q[{index}]: id side is empty in {arg!r}"
        )
    if "\n" in ans:
        raise ClarifyAnswerParseError(
            f"--q[{index}]: answer contains a newline; use --answer-file "
            f"or --stdin for multi-line answers (got {arg!r})"
        )
    return {"id": qid, "answer": ans}


def _parse_q_flags(args: Sequence[str]) -> List[dict]:
    """Parse a sequence of ``--q <id>=<answer>`` flag values.

    Convenience wrapper around :func:`_parse_q_flag` that preserves the
    positional index in error messages. Empty list returns empty list
    so callers can pass ``[]`` unconditionally.
    """
    out: List[dict] = []
    for idx, raw in enumerate(args):
        out.append(_parse_q_flag(raw, index=idx))
    return out


def _parse_json_payload(payload: str, *, source: str = "json") -> List[dict]:
    """Parse a JSON string into a ``[{id, answer}, ...]`` list.

    Accepts the canonical shape ``[{id, answer}, ...]`` AND the single-
    object shape ``{answers: [...]}`` (a concession to past consumers
    that wrapped the list under a key — the JSON shape we accept is
    whatever the operator happens to paste, the validation is what
    matters).

    ``source`` is a free-form label for the error message — pass
    ``"--answer-file <path>"`` or ``"stdin"`` so the user knows where
    the bad payload came from when validation fails.
    """
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ClarifyAnswerParseError(
            f"{source}: invalid JSON ({exc.msg} at line {exc.lineno} col {exc.colno})"
        ) from exc
    # Tolerate the wrapped shape {answers: [...]} — same leniency the
    # rest of the kanban code applies when the operator pastes a
    # slightly-different shape.
    if isinstance(parsed, dict) and "answers" in parsed:
        parsed = parsed["answers"]
    if not isinstance(parsed, list):
        raise ClarifyAnswerParseError(
            f"{source}: expected a JSON array of {{id, answer}} dicts, "
            f"got {type(parsed).__name__}"
        )
    return _validate_answer_list(parsed, source=source)


def _validate_answer_list(items: Iterable, *, source: str) -> List[dict]:
    """Defensive validation of an already-parsed answer iterable.

    Each entry must be a dict with a non-empty string ``id`` and a
    string ``answer``. Empty string answers are allowed (operator said
    "skip this" or pasted whitespace — the fold layer handles blanks).
    """
    out: List[dict] = []
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            raise ClarifyAnswerParseError(
                f"{source}[{idx}]: expected a dict, got {type(item).__name__}"
            )
        qid = item.get("id")
        ans = item.get("answer")
        if not isinstance(qid, str) or not qid.strip():
            raise ClarifyAnswerParseError(
                f"{source}[{idx}]: 'id' must be a non-empty string"
            )
        if not isinstance(ans, str):
            raise ClarifyAnswerParseError(
                f"{source}[{idx}].answer must be a string "
                f"(got {type(ans).__name__})"
            )
        out.append({"id": qid.strip(), "answer": ans})
    if not out:
        raise ClarifyAnswerParseError(
            f"{source}: at least one answer is required"
        )
    return out


def _parse_stdin_json(stream: Optional[TextIO] = None) -> List[dict]:
    """Read a JSON answer payload from stdin.

    ``stream`` defaults to :data:`sys.stdin` so tests can pass a
    :class:`io.StringIO` directly. Skips leading whitespace; an empty
    stream raises :class:`ClarifyAnswerParseError` so a forgotten pipe
    doesn't silently submit zero answers.
    """
    s = stream if stream is not None else sys.stdin
    raw = s.read()
    if not raw.strip():
        raise ClarifyAnswerParseError(
            "stdin: empty input (pipe a JSON array or pass --q / --answer-file)"
        )
    return _parse_json_payload(raw, source="stdin")


def _parse_answer_file(path: str) -> List[dict]:
    """Read a JSON answer payload from a file.

    Companion to :func:`_parse_stdin_json` — same JSON shape, just
    read from disk so the operator can edit a file in their editor
    instead of piping through the terminal.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except OSError as exc:
        raise ClarifyAnswerParseError(
            f"--answer-file {path}: cannot read ({exc.strerror or exc})"
        ) from exc
    if not raw.strip():
        raise ClarifyAnswerParseError(
            f"--answer-file {path}: file is empty"
        )
    return _parse_json_payload(raw, source=f"--answer-file {path}")


def _merge_answer_sources(*sources: List[dict]) -> List[dict]:
    """Merge answers from multiple input sources with last-write-wins on id.

    The CLI accepts ``--q``, ``--answer-file``, and ``--stdin`` in any
    combination; this helper concatenates them so a user can set defaults
    in a file and override one on the command line. Duplicate ids are
    tolerated (last source wins) rather than rejected — the
    flag-vs-flag pattern is "override the file's answer for q1", not
    "the operator made a typo".
    """
    out: List[dict] = []
    by_id: dict[str, int] = {}
    for batch in sources:
        for entry in batch:
            qid = entry["id"]
            if qid in by_id:
                out[by_id[qid]] = entry
            else:
                by_id[qid] = len(out)
                out.append(entry)
    return out


def submit_clarification_answers(
    task_id: str,
    answers: List[dict],
    *,
    author: Optional[str] = None,
) -> Optional[str]:
    """Write answers back to a parked triage task and release it.

    Thin facade over :func:`kanban_db.fold_clarification_answers` so the
    chat-webhook half (a future card) can wire straight to this entry
    point without going through argparse. The validation in
    :func:`_validate_answer_list` already guards the user-input shape;
    the DB layer's own validation is the second line of defense for
    programmatic callers that bypass this module.

    Returns the new task body on success, ``None`` if the task is not
    found OR is not in ``status=awaiting_clarification``. Raises
    :class:`ValueError` if ``answers`` is empty or malformed (caller
    should treat that as a 4xx-class user error).

    The ``author`` argument is recorded on the audit comment so the
    next watcher / specifier tick can show who folded the answers.
    Programmatic callers can pass ``None`` to skip the comment thread.
    """
    # Validate here too so a programmatic caller that bypassed the CLI
    # parser gets a useful error rather than a DB-level traceback.
    answers = _validate_answer_list(answers, source="answers")
    with kb.connect_closing() as conn:
        return kb.fold_clarification_answers(
            conn, task_id, answers=answers, author=author,
        )