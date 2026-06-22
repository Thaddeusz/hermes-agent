"""Kanban triage clarification — turn a rough triage card into 2–3
targeted clarifying questions.

Used by the triage watcher when a task lands in the Triage column. The
watcher calls :func:`generate_clarification_questions`, parks the task
in ``status=awaiting_clarification``, and delivers the questions to the
operator. DB writes and delivery are the watcher's job; this module
does ONE thing: build the prompt, hit the auxiliary LLM, and return a
validated list of question dicts.

Design notes
------------

* This module mirrors ``hermes_cli/kanban_specify.py`` — same aux
  client pattern, same "no aux client configured => skip" tolerance,
  same lenient JSON parsing (tolerates markdown code fences around
  the JSON). Keeps the surface area tiny and the failure modes
  predictable across the two LLM-driven triage helpers.

* The prompt asks for a JSON array ``[{id, question, why_we_ask}, ...]``
  and explicitly biases the model toward questions that would CHANGE
  the implementation (scope, audience, constraints, success criteria)
  rather than trivia the worker can answer on its own.

* Output length is hard-capped to ``max_questions`` even if the model
  overshoots — defensive trim, not a silent fallback.

* On parse failure this module RAISES ``TriageClarifyError`` with the
  raw model output attached for debugging. The watcher treats that as
  a real failure: silently swallowing and returning ``[]`` would
  leave the operator staring at an empty ``clarification_questions``
  column with no clue what went wrong.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from utils import env_int

# Generous ceiling: each clarifying question is a couple of sentences
# plus a ``why_we_ask`` rationale. 1500 tokens covers ~6 questions with
# JSON structure, which gives the model room to overshoot before the
# defensive trim kicks in. The watcher hard-caps to ``max_questions``
# after parsing, so this just needs to be "enough".
HERMES_TRIAGE_CLARIFY_MAX_TOKENS = max(
    800,
    env_int("HERMES_TRIAGE_CLARIFY_MAX_TOKENS", 1500),
)

logger = logging.getLogger(__name__)


class TriageClarifyError(RuntimeError):
    """Raised when the clarification-question LLM call fails or returns
    output that cannot be parsed into a validated question list.

    The watcher treats this as a real failure (it surfaces the task
    on the board as needing attention rather than parking it with an
    empty ``clarification_questions`` column). The raw model output is
    always attached for debugging.
    """

    def __init__(self, message: str, *, raw_output: str = ""):
        super().__init__(message)
        self.raw_output = raw_output

    def __str__(self) -> str:  # pragma: no cover — debug aid
        if not self.raw_output:
            return super().__str__()
        preview = self.raw_output if len(self.raw_output) <= 200 else self.raw_output[:200] + "…"
        return f"{super().__str__()} (raw_output={preview!r})"


_SYSTEM_PROMPT = """You are the triage clarification assistant for the Hermes Agent kanban board.

A user dropped a rough idea into the Triage column. It is too vague to
dispatch to a worker as-is. Your job is to surface the 2–3 questions
whose answers would MOST CHANGE the implementation a worker writes.

Output a single JSON array of question objects, in priority order:

  [
    {
      "id":          "q1",
      "question":    "<one targeted question, plain language>",
      "why_we_ask":  "<one sentence: what part of the plan hinges on this>"
    },
    ...
  ]

Rules:

- Bias toward questions that would change the implementation:
    - scope (what's in vs out)
    - target audience / user
    - hard constraints (platform, perf budget, dependency policy)
    - success criteria (how will the operator know it worked?)
- AVOID trivia the worker can answer on its own (naming conventions,
  file layout, "which library is best" without a constraint that would
  decide it).
- Each ``question`` is a single sentence ending with a question mark.
- Each ``why_we_ask`` is a single short sentence (<= 25 words).
- IDs are ``q1``, ``q2``, ``q3`` in order — no need to invent slugs.
- Output ONLY the JSON array. No preamble, no closing remarks, no
  code fences around the JSON.
"""


_USER_TEMPLATE = """Task title: {title}
Task body:
{body}

Generate up to {max_questions} clarifying questions for this card.
"""


# Reuse the same fence-stripping pattern as kanban_specify so tests
# can share fixtures if we ever consolidate.
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def _extract_json_blob(raw: str) -> Optional[Any]:
    """Lenient JSON extraction — tolerates fenced code blocks and
    leading/trailing prose. Returns the parsed JSON value or None.

    Unlike :func:`hermes_cli.kanban_specify._extract_json_blob`, this
    helper does NOT require the parsed value to be a dict — the
    clarification endpoint returns a top-level array.
    """
    if not raw:
        return None
    stripped = _FENCE_RE.sub("", raw.strip())
    # Greedy: find the first ``[`` and last ``]`` first (the model
    # is asked for a top-level array). Fall back to ``{``/``}`` only
    # if no brackets are present. Some models wrap the array in an
    # outer object — ``{"questions": [...]}`` — and we want the
    # inner array to parse correctly so the downstream ``isinstance``
    # check can enforce the top-level-array contract with a clear
    # error message.
    first = stripped.find("[")
    last = stripped.rfind("]")
    if first != -1 and last != -1 and last > first:
        candidate = stripped[first : last + 1]
        try:
            return json.loads(candidate)
        except (ValueError, json.JSONDecodeError):
            pass
    first = stripped.find("{")
    last = stripped.rfind("}")
    if first != -1 and last != -1 and last > first:
        candidate = stripped[first : last + 1]
        try:
            return json.loads(candidate)
        except (ValueError, json.JSONDecodeError):
            pass
    return None


def _validate_question_entry(entry: Any, index: int) -> Dict[str, str]:
    """Coerce one model output entry into ``{id, question, why_we_ask}``.

    Raises :class:`TriageClarifyError` if any required key is missing
    or not a non-empty string after stripping. The index is included
    in the message so the operator can locate the offending entry in
    the raw output.
    """
    if not isinstance(entry, dict):
        raise TriageClarifyError(
            f"entry #{index} is not an object (got {type(entry).__name__})",
        )
    missing = [k for k in ("id", "question", "why_we_ask") if k not in entry]
    if missing:
        raise TriageClarifyError(
            f"entry #{index} missing required keys: {missing}",
        )
    out: Dict[str, str] = {}
    for key in ("id", "question", "why_we_ask"):
        val = entry[key]
        if not isinstance(val, str) or not val.strip():
            raise TriageClarifyError(
                f"entry #{index} key {key!r} must be a non-empty string "
                f"(got {type(val).__name__})",
            )
        out[key] = val.strip()
    return out


def generate_clarification_questions(
    title: str,
    body: str,
    max_questions: int = 3,
    *,
    timeout: Optional[int] = None,
) -> List[Dict[str, str]]:
    """Return a list of ``{id, question, why_we_ask}`` dicts for a triage card.

    Args:
        title: The triage task's title (always non-empty per the
            watcher contract).
        body: The triage task's body. May be empty (1-line cards are
            the common case). Treated as plain text — the model is
            asked to bias questions toward scope/constraints regardless
            of how much context is present.
        max_questions: Hard cap on the number of questions returned.
            The model is asked for "up to {max_questions}"; the result
            is trimmed to that count even if the model overshoots.
            Must be >= 1.
        timeout: Optional LLM call timeout in seconds. Defaults to
            the auxiliary client's per-task default.

    Returns:
        A list of question dicts, each with ``id``, ``question``, and
        ``why_we_ask`` keys. Length is in ``[0, max_questions]`` —
        zero is a valid result (the model may decide no clarification
        is needed) and is the watcher's signal to skip the
        clarification step rather than block on a question card.

    Raises:
        TriageClarifyError: The LLM call returned output that cannot
            be parsed as a validated question list. The raw output is
            attached to the exception. The watcher should treat this
            as a real failure.
        ValueError: ``max_questions < 1``.
        RuntimeError: Auxiliary client unavailable, propagated from
            the underlying client. The watcher should treat this as
            a real failure (no fallback to "zero questions").
    """
    if max_questions < 1:
        raise ValueError(f"max_questions must be >= 1 (got {max_questions})")

    try:
        from agent.auxiliary_client import (
            get_auxiliary_extra_body,
            get_text_auxiliary_client,
        )
    except Exception as exc:  # pragma: no cover — import smoke test
        raise TriageClarifyError(
            f"auxiliary client import failed: {exc}",
        ) from exc

    try:
        client, model = get_text_auxiliary_client("triage_specifier")
    except Exception as exc:
        raise TriageClarifyError(
            f"get_text_auxiliary_client failed: {exc}",
        ) from exc

    if client is None or not model:
        # Mirror kanban_specify: a missing aux client is configuration,
        # not an LLM error, but the watcher should still see it as a
        # hard failure rather than silently return [].
        raise TriageClarifyError("no auxiliary client configured")

    user_msg = _USER_TEMPLATE.format(
        title=(title or "").strip()[:400] or "(no title)",
        body=(body or "").strip()[:4000] or "(no body — please ask for context)",
        max_questions=max_questions,
    )

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.3,
            max_tokens=HERMES_TRIAGE_CLARIFY_MAX_TOKENS,
            timeout=timeout or 60,
            extra_body=get_auxiliary_extra_body() or None,
        )
    except Exception as exc:
        raise TriageClarifyError(
            f"LLM API call failed: {type(exc).__name__}: {exc}",
        ) from exc

    try:
        raw = (resp.choices[0].message.content or "").strip()
    except Exception as exc:
        raise TriageClarifyError(
            f"LLM returned an unparsable response object: "
            f"{type(exc).__name__}: {exc}",
            raw_output=str(resp)[:500],
        ) from exc

    if not raw:
        raise TriageClarifyError("LLM returned an empty response")

    parsed = _extract_json_blob(raw)
    if parsed is None:
        raise TriageClarifyError(
            "LLM response could not be parsed as JSON",
            raw_output=raw,
        )

    if not isinstance(parsed, list):
        raise TriageClarifyError(
            f"LLM response is not a JSON array (got {type(parsed).__name__})",
            raw_output=raw,
        )

    validated: List[Dict[str, str]] = []
    for index, entry in enumerate(parsed):
        validated.append(_validate_question_entry(entry, index))
        if len(validated) >= max_questions:
            # Defensive trim — the model was asked for "up to N" but
            # can still overshoot. Stop as soon as we have enough.
            break

    return validated


__all__ = [
    "TriageClarifyError",
    "generate_clarification_questions",
    "HERMES_TRIAGE_CLARIFY_MAX_TOKENS",
]
