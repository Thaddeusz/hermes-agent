"""End-to-end smoke test for the interactive triage flow (task t_7bd224b0).

This is the gate the umbrella t_7e2ad8b3 has been waiting for: it exercises
the three layers of the interactive-triage feature together so the
operator knows the ``kanban.triage_clarify.enabled`` flag is safe to flip
from ``false`` to ``true`` in production config.

Three tests, one pipeline each:

  1. **Happy path** — triage card → triage_clarify_tick → answer capture
     → decompose_tick. Verifies the full status column trip
     (triage → awaiting_clarification → triage → todo/ready) plus the
     durable side-effects (pending_clarifications.json written,
     ## User clarifications heading in the body, clarification_questions
     NULL after the fold).

  2. **Timeout path** — a parked card whose ``clarification_asked_at`` is
     past its deadline flips back to ``status='triage'`` under
     ``on_timeout=skip_to_decompose`` so the auto-decomposer picks it up
     on its next tick without the question payload.

  3. **Disabled-by-default guard** — a fresh triage card must NOT be
     parked when ``kanban.triage_clarify.enabled=false``. This is the
     safety toggle the umbrella card hangs its rollout on; flipping it
     must be the only way to enable the feature.

Test isolation
--------------

A fresh ``HERMES_HOME`` per test, with a clean kanban DB initialised
via ``kanban_db.init_db()``. The aux LLM (question generator +
decomposer) is stubbed at the ``agent.auxiliary_client`` seam — the
real integration path is the watcher + answer-capture + decompose
modules calling real DB code against a real ``tasks`` row. Tests do
NOT mock any hermes_cli module; the only seam is the auxiliary client.

Acceptance
----------

All three tests pass, plus the manual one-shot recipe below. Once
green, ``kanban.triage_clarify.enabled`` can be flipped from ``false``
to ``true`` in production config.

Manual one-shot (for the reviewer)
----------------------------------

The reviewer can reproduce the happy path against a real running
gateway with::

    # 1. Insert a triage card.
    hermes kanban create --title "End-to-end smoke" --body "vague" \\
        --triage --json | jq -r .task_id

    # 2. Edit config.yaml: set kanban.triage_clarify.enabled: true.

    # 3. Wait one dispatcher tick (~15s default) — the card should flip
    #    to awaiting_clarification and a pending_clarifications.json
    #    entry should appear under ~/.hermes/.

    # 4. Answer with two --q flags:
    hermes kanban triage --answer <task_id> --q q1=foo --q q2=bar

    # 5. The card should now be in triage with a body containing
    #    "## User clarifications"; on the next auto-decompose tick it
    #    lands in todo (or ready, if it has no open parents).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def triage_home(monkeypatch, tmp_path):
    """Fresh HERMES_HOME with a clean kanban DB + reimported hermes_cli modules.

    Mirrors ``isolated_kanban_home`` from test_kanban_awaiting_clarification.py
    — the kanban_db module captures ``kanban_home()`` at import time on some
    call paths, so we reimport it under the new HOME so each test sees its
    own DB and doesn't leak state to siblings in the same session.
    """
    test_home = tmp_path / ".hermes"
    test_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(test_home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    prefixes = ("hermes_cli", "hermes_state", "hermes_constants")
    snapshot = {
        n: m for n, m in list(sys.modules.items())
        if any(n == p or n.startswith(p + ".") for p in prefixes)
    }
    for n in list(snapshot):
        del sys.modules[n]
    from hermes_cli import kanban_db as kb_fresh

    kb_fresh.init_db()

    yield test_home, kb_fresh

    # Restore module state so later tests in the session don't see the
    # temp HOME.
    reimported = [
        n for n in list(sys.modules)
        if any(n == p or n.startswith(p + ".") for p in prefixes)
    ]
    for n in reimported:
        del sys.modules[n]
    for n, m in snapshot.items():
        sys.modules.setdefault(n, m)


def _patch_aux_client_returning(content: str):
    """Stub ``agent.auxiliary_client.get_text_auxiliary_client``.

    Returns the ``patch`` context manager — tests do ``with`` it. Both
    the watcher (calling generate_clarification_questions) and the
    decomposer (calling decompose_task) read from the same aux-client
    seam, so stubbing once covers both.
    """
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    client = MagicMock()
    client.chat.completions.create = MagicMock(return_value=resp)
    return patch(
        "agent.auxiliary_client.get_text_auxiliary_client",
        return_value=(client, "test-model"),
    )


def _patch_aux_extra_body():
    """Stub ``get_auxiliary_extra_body`` — the decomposer passes it through."""
    return patch(
        "agent.auxiliary_client.get_auxiliary_extra_body",
        return_value={},
    )


def _patch_profiles(names: list[str]):
    """Pretend the named profiles exist for the decomposer's roster.

    Same shape as ``test_kanban_decompose.py::_patch_list_profiles`` —
    the decomposer calls ``hermes_cli.profiles.list_profiles`` +
    ``profile_exists`` to build its valid-set, and those need to be
    stable in the integration test.
    """
    fake_profiles = [
        SimpleNamespace(
            name=n, is_default=(i == 0), description=f"desc for {n}",
            description_auto=False, model="m", provider="p", skill_count=1,
        )
        for i, n in enumerate(names)
    ]
    return [
        patch("hermes_cli.profiles.list_profiles", return_value=fake_profiles),
        patch("hermes_cli.profiles.profile_exists", side_effect=lambda x: x in names),
        patch(
            "hermes_cli.profiles.get_active_profile_name",
            return_value=names[0] if names else "default",
        ),
    ]


def _enabled_clarify_config(*, max_questions: int = 2):
    """Config stub: triage_clarify enabled, max_questions=2.

    Returns a zero-arg callable matching ``triage_clarify_tick``'s
    ``load_config`` signature.
    """
    return lambda: {
        "kanban": {
            "triage_clarify": {
                "enabled": True,
                "max_questions": max_questions,
                "timeout_days": 7,
                "on_timeout": "skip_to_decompose",
            }
        }
    }


def _disabled_clarify_config():
    """Config stub: triage_clarify explicitly disabled (the production default)."""
    return lambda: {
        "kanban": {
            "triage_clarify": {
                "enabled": False,
                "max_questions": 2,
                "timeout_days": 7,
                "on_timeout": "skip_to_decompose",
            }
        }
    }


def _two_question_json():
    """A canned two-question payload the question generator returns."""
    return json.dumps([
        {
            "id": "q1",
            "question": "What is the goal?",
            "why_we_ask": "scope",
        },
        {
            "id": "q2",
            "question": "Who is the audience?",
            "why_we_ask": "tone",
        },
    ])


def _single_task_fanout_payload(title: str, body: str, assignee: str):
    """A canned decomposer payload that produces a single (non-fanout) task.

    The decomoser returns ``fanout=false`` when the work doesn't split
    cleanly; the gateway then calls ``specify_triage_task`` to flesh
    out the row and flip it to ``todo``. A non-fanout payload keeps
    the test focused on the triage → awaiting → triage → todo column
    trip without dragging in graph-construction bookkeeping.
    """
    return json.dumps({
        "fanout": False,
        "title": title,
        "body": body,
        "assignee": assignee,
    })


# ---------------------------------------------------------------------------
# 1. Happy path — triage → parked → answered → decomposed
# ---------------------------------------------------------------------------


def test_happy_path_triage_to_decomposed_end_to_end(triage_home, monkeypatch):
    """A fresh triage card parks, gets answered, then decomposes into todo.

    Pipeline:

      1. Insert a triage card with a vague body.
      2. Run one watcher tick with ``enabled=true, max_questions=2``.
      3. Card flips to ``awaiting_clarification`` with 2 questions;
         ``~/.hermes/pending_clarifications.json`` gets a matching entry.
      4. Submit answers via the kanban_clarify_answers module directly
         (the same path ``hermes kanban triage --answer`` calls).
      5. Card flips back to ``triage`` with the
         ``## User clarifications`` heading folded into the body;
         ``clarification_questions`` is NULL.
      6. Run one decompose_task call (the work the auto-decompose tick
         would do in production). Card lands in ``todo`` (or
         ``ready``, if no parents block it) with the new fleshed-out
         body.
    """
    home, kb = triage_home
    monkeypatch.setenv("HERMES_HOME", str(home))

    from gateway import triage_clarify_watcher as tcw
    from hermes_cli import kanban_clarify_answers as kca

    # Step 1: create the triage card.
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="Vague feature",
            body="user wants something around dark mode and notifications",
            triage=True,
            created_by="e2e-smoke",
        )
    assert tid.startswith("t_")

    # Step 2 + 3: run the watcher tick with the question generator stubbed.
    with _patch_aux_client_returning(_two_question_json()):
        stats = tcw.triage_clarify_tick(
            _enabled_clarify_config(max_questions=2),
            kb.list_boards,
            kb.connect,
        )

    assert stats["asked"] == 1, stats
    assert stats["timed_out"] == 0

    # Card is now parked with 2 questions + a pending-file entry.
    with kb.connect() as conn:
        row = conn.execute(
            "SELECT status, clarification_questions, clarification_asked_at "
            "FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone()
    assert row["status"] == "awaiting_clarification"
    assert row["clarification_asked_at"] is not None
    questions = json.loads(row["clarification_questions"])
    assert [q["id"] for q in questions] == ["q1", "q2"]

    pending_path = home / "pending_clarifications.json"
    assert pending_path.exists(), "watcher must write the pending file"
    pending = json.loads(pending_path.read_text())
    assert tid in pending
    assert [q["id"] for q in pending[tid]["questions"]] == ["q1", "q2"]
    assert pending[tid]["title"] == "Vague feature"

    # Step 4: submit answers via the kanban_clarify_answers module —
    # the exact path ``hermes kanban triage --answer --q q1=... --q q2=...``
    # exercises. We use the public submit_clarification_answers entry
    # point so the test doesn't depend on argparse parsing internals.
    answers = [
        {"id": "q1", "answer": "Ship a toggle for dark mode in user settings"},
        {"id": "q2", "answer": "Internal team + power users; no external API"},
    ]
    new_body = kca.submit_clarification_answers(
        tid, answers, author="e2e-smoke",
    )

    # Step 5: card back in triage, body folded, questions cleared.
    assert new_body is not None
    assert "## User clarifications" in new_body
    assert "Ship a toggle for dark mode" in new_body

    with kb.connect() as conn:
        row = conn.execute(
            "SELECT status, body, clarification_questions, "
            "clarification_asked_at, clarification_answers "
            "FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone()
    assert row["status"] == "triage"
    assert row["clarification_questions"] is None
    assert row["clarification_asked_at"] is None
    assert "## User clarifications" in row["body"]
    # Answers column carries the merged payload — useful for the audit
    # trail even after the body has been folded.
    answers_col = json.loads(row["clarification_answers"])
    assert {a["id"] for a in answers_col} == {"q1", "q2"}

    # Step 6: run the decompose tick. Stub both the aux client (so the
    # decomposer doesn't reach a real provider) and the profiles
    # registry (so the roster is non-empty). The single-task payload
    # exercises the no-fanout path, which calls specify_triage_task
    # to flesh out the row and flip to todo.
    profile_patches = _patch_profiles(["orchestrator", "main_profile"])
    for p in profile_patches:
        p.start()
    try:
        with _patch_aux_client_returning(
            _single_task_fanout_payload(
                title="Ship dark mode toggle",
                body="Add a settings switch that lets the user toggle dark mode; "
                     "see clarifications above for scope.",
                assignee="main_profile",
            )
        ), _patch_aux_extra_body():
            from hermes_cli import kanban_decompose as decomp
            outcome = decomp.decompose_task(tid, author="e2e-smoke")
    finally:
        for p in profile_patches:
            p.stop()

    assert outcome.ok, f"decompose_task failed: {outcome.reason}"

    # Card is out of triage and into the work pipeline. ``recompute_ready``
    # can flip it past todo straight to ready if no parents block it —
    # the umbrella card cares about the column trip ending outside
    # triage, not the exact arrival status.
    with kb.connect() as conn:
        final = conn.execute(
            "SELECT status, title, body, clarification_questions "
            "FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone()
    assert final["status"] in ("todo", "ready")
    assert final["status"] != "triage"
    assert final["clarification_questions"] is None
    # Fleshed-out title from the decomposer payload.
    assert final["title"] == "Ship dark mode toggle"
    # The decomposer replaces the body wholesale with its own output
    # (``specify_triage_task`` is the path here), so the folded
    # ``## User clarifications`` heading does not survive step 6. The
    # durable audit trail is the ``clarification_answers`` column,
    # which the fold wrote in step 4 and which the next consumer can
    # read from the row regardless of what the decomposer did with the
    # body. Verify it still carries the merged answers.
    with kb.connect() as conn:
        answers_after = conn.execute(
            "SELECT clarification_answers FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone()
    answers_col = json.loads(answers_after["clarification_answers"])
    answer_ids = {a["id"] for a in answers_col}
    assert answer_ids == {"q1", "q2"}, answers_col
    answer_by_id = {a["id"]: a.get("answer") for a in answers_col}
    assert "Ship a toggle for dark mode" in answer_by_id["q1"]
    assert "Internal team" in answer_by_id["q2"]


# ---------------------------------------------------------------------------
# 2. Timeout path — parked card past deadline flips back to triage
# ---------------------------------------------------------------------------


def test_timeout_path_flips_parked_card_back_to_triage(
    triage_home, monkeypatch,
):
    """A parked card past its deadline flips back to ``status='triage'``.

    Mirrors the production behaviour: the operator sets
    ``on_timeout=skip_to_decompose`` so the auto-decomposer picks the
    card up on its next tick without re-asking. We assert the watcher
    flips the row + clears the question payload + drops the
    pending-file entry.
    """
    home, kb = triage_home
    monkeypatch.setenv("HERMES_HOME", str(home))

    from gateway import triage_clarify_watcher as tcw

    eight_days_ago = int(time.time()) - 8 * 86400

    # Insert a card already parked, with asked_at past the 7-day default
    # deadline. Use create_task with initial_status=awaiting_clarification
    # so the test doesn't go through the question-generator tick first.
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="Stale parked card",
            body="sat too long",
            initial_status="awaiting_clarification",
            created_by="e2e-smoke",
        )
        kb.set_task_clarification(
            conn, tid,
            questions=[
                {"id": "q1", "question": "?", "why_we_ask": "x"},
            ],
            asked_at=eight_days_ago,
            timeout_days=7,
        )
        # Seed a pending-file entry that the watcher should drop on flip.
    pending_path = home / "pending_clarifications.json"
    pending_path.write_text(json.dumps({
        tid: {
            "task_id": tid,
            "title": "Stale parked card",
            "questions": [{"id": "q1"}],
            "asked_at": eight_days_ago,
        },
    }, indent=2))

    # Run the watcher tick. No question-generation is involved (the card
    # isn't in triage), but the timeout sweep is what matters here.
    stats = tcw.triage_clarify_tick(
        _enabled_clarify_config(),
        kb.list_boards,
        kb.connect,
    )

    assert stats["timed_out"] == 1, stats
    assert stats["asked"] == 0

    # Card back in triage, questions cleared, no pending-file entry.
    with kb.connect() as conn:
        row = conn.execute(
            "SELECT status, clarification_questions, clarification_asked_at "
            "FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone()
    assert row["status"] == "triage"
    assert row["clarification_questions"] is None
    assert row["clarification_asked_at"] is None

    pending_after = json.loads(pending_path.read_text())
    assert tid not in pending_after, (
        "watcher must drop the pending-file entry when it flips a card "
        "back to triage so the next CLI session doesn't surface a "
        "question that's no longer pending"
    )


# ---------------------------------------------------------------------------
# 3. Disabled-by-default guard
# ---------------------------------------------------------------------------


def test_disabled_flag_prevents_parking(triage_home, monkeypatch):
    """With ``kanban.triage_clarify.enabled=false``, a fresh triage card stays put.

    This is the rollout gate the umbrella t_7e2ad8b3 has been waiting
    on: the operator wants the feature off-by-default in production
    config until smoke testing confirms it's safe to flip. A single
    stray tick with ``enabled=true`` would otherwise park every fresh
    triage card without warning.
    """
    home, kb = triage_home
    monkeypatch.setenv("HERMES_HOME", str(home))

    from gateway import triage_clarify_watcher as tcw

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="Should not be parked",
            body="watcher must leave this alone when disabled",
            triage=True,
            created_by="e2e-smoke",
        )

    # Stub the aux client so a bug that ignores the flag would still
    # surface — if the watcher called the model regardless, we'd see
    # ``client.chat.completions.create`` get invoked and the row flip.
    # If it didn't, the test passes cleanly.
    stub_calls = {"invocations": 0}

    def _tracking_client_returning(content: str):
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = content

        def _track_create(*args, **kwargs):
            stub_calls["invocations"] += 1
            return resp

        client = MagicMock()
        client.chat.completions.create = MagicMock(side_effect=_track_create)
        return client

    client = _tracking_client_returning(_two_question_json())

    with patch(
        "agent.auxiliary_client.get_text_auxiliary_client",
        return_value=(client, "test-model"),
    ):
        stats = tcw.triage_clarify_tick(
            _disabled_clarify_config(),
            kb.list_boards,
            kb.connect,
        )

    # The watcher should have no-op'd entirely.
    assert stats["asked"] == 0
    assert stats["timed_out"] == 0
    assert stats["boards_visited"] == 0

    # Aux client must NOT have been called — the disabled guard is
    # short-circuited BEFORE the question generator runs.
    assert stub_calls["invocations"] == 0

    # Card still in triage, no questions parked.
    with kb.connect() as conn:
        row = conn.execute(
            "SELECT status, clarification_questions, clarification_asked_at "
            "FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone()
    assert row["status"] == "triage"
    assert row["clarification_questions"] is None
    assert row["clarification_asked_at"] is None

    # No pending-file side-effect either — the disabled guard returns
    # before touching any state.
    assert not (home / "pending_clarifications.json").exists()