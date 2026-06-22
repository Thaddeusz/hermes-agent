"""Tests for the triage clarification question generator (task t_92f8c0f0).

These tests mock the auxiliary LLM client — they never hit a network or
real provider. They cover the prompt plumbing, JSON extraction,
validation, defensive trimming, and the exception path required by the
task spec.
"""

from __future__ import annotations

import json as jsonlib
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import triage_clarify as tc


# ---------------------------------------------------------------------------
# Aux-client mocking helpers
# ---------------------------------------------------------------------------


def _fake_aux_response(content: str):
    """Build a minimal object shaped like an OpenAI chat.completions result."""
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    return resp


def _mock_client_returning(content: str):
    client = MagicMock()
    client.chat.completions.create = MagicMock(return_value=_fake_aux_response(content))
    return client


def _patch_aux_client(content: str, *, model: str = "test-model"):
    """Patch get_text_auxiliary_client at its source. The module imports
    it lazily inside generate_clarification_questions, so patching the
    source attribute is sufficient (same pattern as test_kanban_specify).
    """
    client = _mock_client_returning(content)
    return patch(
        "agent.auxiliary_client.get_text_auxiliary_client",
        return_value=(client, model),
    ), client


# ---------------------------------------------------------------------------
# JSON extraction helper
# ---------------------------------------------------------------------------


def test_extract_json_blob_handles_plain_array():
    raw = '[{"id": "q1", "question": "Q?", "why_we_ask": "because"}]'
    out = tc._extract_json_blob(raw)
    assert isinstance(out, list)
    assert len(out) == 1
    assert out[0]["id"] == "q1"


def test_extract_json_blob_handles_fenced_array():
    raw = '```json\n[{"id": "q1", "question": "Q?", "why_we_ask": "because"}]\n```'
    out = tc._extract_json_blob(raw)
    assert isinstance(out, list)
    assert len(out) == 1


def test_extract_json_blob_handles_prose_preamble():
    raw = 'Sure! Here you go:\n[{"id": "q1", "question": "Q?", "why_we_ask": "because"}]\nThanks.'
    out = tc._extract_json_blob(raw)
    assert isinstance(out, list)
    assert len(out) == 1


def test_extract_json_blob_returns_none_for_unparseable():
    assert tc._extract_json_blob("no json here") is None
    assert tc._extract_json_blob("") is None
    assert tc._extract_json_blob("[not: valid]") is None


def test_extract_json_blob_finds_inner_array_inside_object():
    # The greedy `[`/`]` scan picks up inner arrays inside wrapper objects,
    # so a model that emits ``{"questions": [...]}`` still parses. The
    # downstream ``isinstance(parsed, list)`` check then enforces the
    # top-level-array contract — see test_..._top_level_object_raises.
    raw = '{"questions": [{"id": "q1", "question": "Q?", "why_we_ask": "w"}]}'
    out = tc._extract_json_blob(raw)
    assert isinstance(out, list)
    assert len(out) == 1


# ---------------------------------------------------------------------------
# Entry validation
# ---------------------------------------------------------------------------


def test_validate_question_entry_happy():
    out = tc._validate_question_entry(
        {"id": "q1", "question": "Q?", "why_we_ask": "because"}, 0,
    )
    assert out == {"id": "q1", "question": "Q?", "why_we_ask": "because"}


def test_validate_question_entry_strips_whitespace():
    out = tc._validate_question_entry(
        {"id": "  q1  ", "question": "  Q?  ", "why_we_ask": "  because  "}, 0,
    )
    assert out == {"id": "q1", "question": "Q?", "why_we_ask": "because"}


def test_validate_question_entry_rejects_non_object():
    with pytest.raises(tc.TriageClarifyError, match=r"not an object"):
        tc._validate_question_entry("not a dict", 0)


def test_validate_question_entry_rejects_missing_keys():
    with pytest.raises(tc.TriageClarifyError, match=r"missing required keys"):
        tc._validate_question_entry({"id": "q1", "question": "Q?"}, 0)


def test_validate_question_entry_rejects_empty_string():
    with pytest.raises(tc.TriageClarifyError, match=r"non-empty string"):
        tc._validate_question_entry(
            {"id": "q1", "question": "  ", "why_we_ask": "because"}, 0,
        )


# ---------------------------------------------------------------------------
# generate_clarification_questions — acceptance tests from the task spec
# ---------------------------------------------------------------------------


def test_generate_clarification_questions_happy_path():
    """Acceptance test #1: a sample triage card produces a list of dicts
    with id, question, why_we_ask keys, length <= max_questions.
    """
    questions = [
        {"id": "q1", "question": "Which surfaces need dark mode?", "why_we_ask": "scope"},
        {"id": "q2", "question": "OS preference (system / light / dark)?", "why_we_ask": "default"},
        {"id": "q3", "question": "Must it persist per user or per device?", "why_we_ask": "storage"},
    ]
    p, _ = _patch_aux_client(jsonlib.dumps(questions))
    with p:
        result = tc.generate_clarification_questions(
            title="Add dark mode",
            body="I want a dark mode for the app.",
            max_questions=3,
        )

    assert isinstance(result, list)
    assert len(result) == 3
    assert len(result) <= 3
    for entry in result:
        assert set(entry.keys()) == {"id", "question", "why_we_ask"}
        assert entry["id"].startswith("q")
        assert entry["question"].endswith("?")
        assert entry["why_we_ask"]


def test_generate_clarification_questions_trims_overshoot():
    """Defensive trim: model returns 5, we cap at 3."""
    questions = [
        {"id": f"q{i}", "question": f"Q{i}?", "why_we_ask": "w"} for i in range(1, 6)
    ]
    p, _ = _patch_aux_client(jsonlib.dumps(questions))
    with p:
        result = tc.generate_clarification_questions(
            title="Anything", body="", max_questions=3,
        )
    assert len(result) == 3
    assert [q["id"] for q in result] == ["q1", "q2", "q3"]


def test_generate_clarification_questions_zero_is_valid():
    """An empty list is a valid model response (no clarification needed)
    and the watcher treats it as the signal to skip clarification.
    """
    p, _ = _patch_aux_client("[]")
    with p:
        result = tc.generate_clarification_questions(
            title="Already-detailed task", body="Lots of context.", max_questions=3,
        )
    assert result == []


def test_generate_clarification_questions_malformed_raises():
    """Acceptance test #2: malformed model output raises TriageClarifyError,
    does NOT silently swallow to [].
    """
    p, _ = _patch_aux_client("this is not JSON at all, sorry")
    with p:
        with pytest.raises(tc.TriageClarifyError) as excinfo:
            tc.generate_clarification_questions(
                title="x", body="y", max_questions=3,
            )
    # Raw output must be attached for debugging.
    assert excinfo.value.raw_output == "this is not JSON at all, sorry"
    assert "could not be parsed" in str(excinfo.value)


def test_generate_clarification_questions_missing_keys_raises():
    """An entry missing a required key is a parse failure, not a silent skip."""
    bad = [{"id": "q1", "question": "Q?"}]  # missing why_we_ask
    p, _ = _patch_aux_client(jsonlib.dumps(bad))
    with p:
        with pytest.raises(tc.TriageClarifyError, match=r"missing required keys"):
            tc.generate_clarification_questions(
                title="x", body="y", max_questions=3,
            )


def test_generate_clarification_questions_top_level_object_raises():
    """A top-level object (with no array inside) is a shape error."""
    p, _ = _patch_aux_client('{"questions": "missing inner array"}')
    with p:
        with pytest.raises(tc.TriageClarifyError, match=r"not a JSON array"):
            tc.generate_clarification_questions(
                title="x", body="y", max_questions=3,
            )


def test_generate_clarification_questions_empty_response_raises():
    p, _ = _patch_aux_client("")
    with p:
        with pytest.raises(tc.TriageClarifyError, match=r"empty response"):
            tc.generate_clarification_questions(
                title="x", body="y", max_questions=3,
            )


def test_generate_clarification_questions_no_aux_client_raises():
    """No aux client configured is a configuration error, not []."""
    with patch(
        "agent.auxiliary_client.get_text_auxiliary_client",
        return_value=(None, ""),
    ):
        with pytest.raises(tc.TriageClarifyError, match=r"no auxiliary client"):
            tc.generate_clarification_questions(
                title="x", body="y", max_questions=3,
            )


def test_generate_clarification_questions_llm_api_error_raises():
    """API errors propagate as TriageClarifyError with the underlying
    exception type in the message — the watcher should treat them as
    real failures, not silently skip.
    """
    client = MagicMock()
    client.chat.completions.create = MagicMock(side_effect=RuntimeError("429"))
    with patch(
        "agent.auxiliary_client.get_text_auxiliary_client",
        return_value=(client, "test-model"),
    ):
        with pytest.raises(tc.TriageClarifyError, match=r"LLM API call failed.*RuntimeError"):
            tc.generate_clarification_questions(
                title="x", body="y", max_questions=3,
            )


def test_generate_clarification_questions_rejects_bad_max():
    """Defensive guard: max_questions < 1 is a programmer error."""
    with pytest.raises(ValueError, match=r"max_questions must be >= 1"):
        tc.generate_clarification_questions(title="x", body="y", max_questions=0)


def test_generate_clarification_questions_tolerates_missing_body():
    """A 1-line card (no body) is the common case — must not crash."""
    questions = [{"id": "q1", "question": "Q?", "why_we_ask": "w"}]
    p, _ = _patch_aux_client(jsonlib.dumps(questions))
    with p:
        result = tc.generate_clarification_questions(
            title="Add dark mode", body="", max_questions=3,
        )
    assert len(result) == 1


def test_generate_clarification_questions_uses_triage_specifier_slot():
    """The model must be resolved via the triage_specifier aux slot
    (per the task spec — the watcher relies on this for per-task
    provider overrides).
    """
    client = _mock_client_returning("[]")
    mock_resolver = MagicMock(return_value=(client, "test-model"))
    with patch(
        "agent.auxiliary_client.get_text_auxiliary_client", mock_resolver,
    ):
        tc.generate_clarification_questions(
            title="x", body="y", max_questions=3,
        )
    mock_resolver.assert_called_once()
    # The task spec requires the triage_specifier slot for per-task
    # provider overrides — the watcher relies on this.
    call = mock_resolver.call_args
    slot = call.args[0] if call.args else call.kwargs.get("task")
    assert slot == "triage_specifier"
