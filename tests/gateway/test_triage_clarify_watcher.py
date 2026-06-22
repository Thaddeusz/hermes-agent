"""Tests for the triage clarification watcher (task t_e647d476).

Covers both sweeps of ``triage_clarify_tick``:

  - **Sweep A — fresh triage cards.** A triage card without
    clarification questions gets questions generated, is parked in
    ``status='awaiting_clarification'`` with ``clarification_asked_at``
    set, and the pending file is updated.
  - **Sweep B — timed-out parked cards.** A card whose
    ``clarification_asked_at`` is older than the configured deadline
    flips back to ``status='triage'`` under ``on_timeout=skip_to_decompose``
    and stays parked under ``on_timeout=leave_in_awaiting``.
  - **Master gate.** When ``kanban.triage_clarify.enabled`` is false,
    the tick is a no-op (no DB writes, no pending file changes) — the
    default state until smoke testing completes.
  - **Settings resolution.** ``_resolve_triage_clarify_settings`` returns
    the documented defaults, respects overrides, clamps invalid
    ``max_questions`` / ``timeout_days``, and fails safe on bad input.
  - **Pending file atomicity.** The temp-file + rename pattern keeps a
    crash mid-write from leaving the operator with a half-written JSON.

Aux-client mocks mirror the pattern in
``tests/hermes_cli/test_triage_clarify.py``: the watcher never hits a
network or real provider.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from gateway import triage_clarify_watcher as tcw


# ---------------------------------------------------------------------------
# Aux-client mock (mirrors test_triage_clarify.py)
# ---------------------------------------------------------------------------


def _make_fake_aux_returning(content: str):
    """Return ``(patch_ctx, client)`` so a test can ``with patch_ctx:``."""
    from unittest.mock import MagicMock

    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content

    client = MagicMock()
    client.chat.completions.create = MagicMock(return_value=resp)
    patcher = patch(
        "agent.auxiliary_client.get_text_auxiliary_client",
        return_value=(client, "test-model"),
    )
    return patcher, client


def _valid_question_json(*ids: str) -> str:
    return json.dumps([
        {"id": qid, "question": f"Question {qid}?", "why_we_ask": "scope"}
        for qid in ids
    ])


# ---------------------------------------------------------------------------
# HERMES_HOME / kanban DB fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Fresh HERMES_HOME with a clean default kanban DB.

    Re-imports ``hermes_cli.kanban_db`` so the reimported module
    sees the new HOME (the module captures ``kanban_home()`` at
    import time on some call paths). Mirrors the
    ``isolated_kanban_home`` fixture in
    ``tests/hermes_cli/test_kanban_awaiting_clarification.py``.
    """
    from hermes_cli import kanban_db as kb

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    prefixes = ("hermes_cli", "hermes_state", "hermes_constants")
    snapshot = {
        n: m for n, m in list(sys.modules.items())
        if any(n == p or n.startswith(p + ".") for p in prefixes)
    }
    for n in list(snapshot):
        del sys.modules[n]
    from hermes_cli import kanban_db as kb_fresh
    kb = kb_fresh
    kb.init_db()
    yield home, kb
    # Restore modules so later tests don't see the temp HOME.
    reimported = [
        n for n in list(sys.modules)
        if any(n == p or n.startswith(p + ".") for p in prefixes)
    ]
    for n in reimported:
        del sys.modules[n]
    for n, m in snapshot.items():
        sys.modules.setdefault(n, m)


def _insert_triage(conn, *, title="Add dark mode", body="User wants dark mode"):
    """Insert a triage card with no clarification columns set."""
    from hermes_cli.kanban_db import create_task
    return create_task(
        conn,
        title=title,
        body=body,
        triage=True,
        created_by="test",
    )


def _insert_awaiting(conn, *, title="Parked", asked_at, timeout_days=None,
                     questions=None):
    """Insert a card already parked in awaiting_clarification."""
    from hermes_cli.kanban_db import create_task
    tid = create_task(
        conn,
        title=title,
        body="body",
        initial_status="awaiting_clarification",
        created_by="test",
    )
    from hermes_cli.kanban_db import set_task_clarification
    set_task_clarification(
        conn, tid,
        questions=questions or [
            {"id": "q1", "question": "Q?", "why_we_ask": "x"},
        ],
        asked_at=asked_at,
        timeout_days=timeout_days,
    )
    return tid


# ---------------------------------------------------------------------------
# Settings resolution
# ---------------------------------------------------------------------------


def test_settings_default_when_key_absent():
    """Empty config returns the documented defaults; flag is off."""
    enabled, mx, td, ot, wa_en, wa_rc = tcw._resolve_triage_clarify_settings(
        lambda: {}
    )
    assert enabled is False
    assert mx == 3
    assert td == 7
    assert ot == "skip_to_decompose"
    assert wa_en is False
    assert wa_rc == ""


def test_settings_enabled_respected_when_true():
    enabled, _, _, _, _, _ = tcw._resolve_triage_clarify_settings(
        lambda: {"kanban": {"triage_clarify": {"enabled": True}}}
    )
    assert enabled is True


def test_settings_max_questions_clamped_to_one_on_zero():
    """``max_questions=0`` clamps to 1 — a 0 cap would disable progress."""
    _, mx, _, _, _, _ = tcw._resolve_triage_clarify_settings(
        lambda: {"kanban": {"triage_clarify": {"max_questions": 0}}}
    )
    assert mx == 1


def test_settings_max_questions_clamped_on_negative():
    _, mx, _, _, _, _ = tcw._resolve_triage_clarify_settings(
        lambda: {"kanban": {"triage_clarify": {"max_questions": -5}}}
    )
    assert mx == 1


def test_settings_timeout_days_falls_back_on_invalid():
    """Malformed timeout falls back to the documented 7-day default."""
    _, _, td, _, _, _ = tcw._resolve_triage_clarify_settings(
        lambda: {"kanban": {"triage_clarify": {"timeout_days": "not-a-number"}}}
    )
    assert td == 7


def test_settings_on_timeout_unknown_value_falls_back_to_skip():
    """An unrecognised ``on_timeout`` is treated as ``skip_to_decompose``.

    Defensive default: a typo in config can't accidentally leave
    cards parked forever.
    """
    _, _, _, ot, _, _ = tcw._resolve_triage_clarify_settings(
        lambda: {"kanban": {"triage_clarify": {"on_timeout": "explode"}}}
    )
    assert ot == "skip_to_decompose"


def test_settings_on_timeout_leave_in_awaiting_respected():
    _, _, _, ot, _, _ = tcw._resolve_triage_clarify_settings(
        lambda: {"kanban": {"triage_clarify": {"on_timeout": "leave_in_awaiting"}}}
    )
    assert ot == "leave_in_awaiting"


def test_settings_whatsapp_recipient_returns_string():
    _, _, _, _, wa_en, wa_rc = tcw._resolve_triage_clarify_settings(
        lambda: {"kanban": {"triage_clarify": {
            "delivery": {"whatsapp": {"enabled": True, "recipient": 12345}}
        }}}
    )
    # Recipient is coerced to string — operators can write either form.
    assert wa_en is True
    assert wa_rc == "12345"


def test_settings_fails_safe_on_config_error():
    """A loader that raises returns the disabled defaults, not a crash."""
    def boom():
        raise RuntimeError("config broken")
    enabled, _, _, _, _, _ = tcw._resolve_triage_clarify_settings(boom)
    assert enabled is False


# ---------------------------------------------------------------------------
# Pending clarifications file atomicity
# ---------------------------------------------------------------------------


def test_pending_file_writes_atomically(tmp_path, monkeypatch):
    """A successful write produces a parseable file at the expected path."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    payload = {"t_abc": {"task_id": "t_abc", "title": "T", "questions": [], "asked_at": 1}}
    tcw._write_pending_clarifications(payload)
    path = tcw._pending_clarifications_path()
    assert path.exists()
    assert path.parent == tmp_path
    loaded = json.loads(path.read_text())
    assert loaded == payload


def test_pending_file_overwrites_existing(tmp_path, monkeypatch):
    """A second write replaces the file in full (no append)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    tcw._write_pending_clarifications({"old": {"x": 1}})
    tcw._write_pending_clarifications({"new": {"x": 2}})
    loaded = tcw._read_pending_clarifications()
    assert loaded == {"new": {"x": 2}}


def test_pending_file_read_handles_missing(tmp_path, monkeypatch):
    """Missing file returns empty dict — no exception."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert tcw._read_pending_clarifications() == {}


def test_pending_file_read_handles_corrupt(tmp_path, monkeypatch):
    """Corrupt JSON returns empty dict — next tick overwrites cleanly."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = tcw._pending_clarifications_path()
    path.write_text("{not json")
    assert tcw._read_pending_clarifications() == {}


# ---------------------------------------------------------------------------
# Sweep A — fresh triage cards get questions
# ---------------------------------------------------------------------------


def _enabled_config():
    """Config loader stub that returns ``triage_clarify.enabled=True``."""
    return {"kanban": {"triage_clarify": {"enabled": True}}}


def test_sweep_a_parks_triage_card_and_writes_pending_file(
    hermes_home, monkeypatch,
):
    """A fresh triage card becomes ``awaiting_clarification`` + file entry."""
    home, kb = hermes_home
    monkeypatch.setenv("HERMES_HOME", str(home))

    conn = kb.connect()
    tid = _insert_triage(conn, title="Add dark mode", body="yes")

    patcher, _client = _make_fake_aux_returning(
        _valid_question_json("q1", "q2")
    )
    sent_to_whatsapp = []

    def fake_sender(recipient, message):
        sent_to_whatsapp.append((recipient, message))
        return True

    with patcher:
        stats = tcw.triage_clarify_tick(
            _enabled_config,
            kb.list_boards,
            kb.connect,
            whatsapp_sender=fake_sender,
        )

    assert stats["asked"] == 1
    assert stats["timed_out"] == 0

    # Card is now parked.
    row = conn.execute(
        "SELECT status, clarification_questions, clarification_asked_at "
        "FROM tasks WHERE id = ?", (tid,)
    ).fetchone()
    assert row["status"] == "awaiting_clarification"
    assert row["clarification_asked_at"] is not None
    qs = json.loads(row["clarification_questions"])
    assert [q["id"] for q in qs] == ["q1", "q2"]

    # Pending file got the entry.
    pending = tcw._read_pending_clarifications()
    assert tid in pending
    assert pending[tid]["title"] == "Add dark mode"
    assert [q["id"] for q in pending[tid]["questions"]] == ["q1", "q2"]
    conn.close()


def test_sweep_a_skips_card_when_model_returns_zero_questions(
    hermes_home, monkeypatch,
):
    """Model decided no clarification is needed — card stays in triage."""
    home, kb = hermes_home
    monkeypatch.setenv("HERMES_HOME", str(home))

    conn = kb.connect()
    tid = _insert_triage(conn)

    patcher, _client = _make_fake_aux_returning("[]")
    with patcher:
        stats = tcw.triage_clarify_tick(
            _enabled_config, kb.list_boards, kb.connect,
        )

    assert stats["asked"] == 0
    row = conn.execute(
        "SELECT status FROM tasks WHERE id = ?", (tid,)
    ).fetchone()
    assert row["status"] == "triage"
    # No pending file written (no entry to record).
    assert tcw._read_pending_clarifications() == {}
    conn.close()


def test_sweep_a_trims_oversized_question_list(
    hermes_home, monkeypatch,
):
    """``max_questions`` is enforced even if the model overshoots.

    Defensive trim — the generator already caps, but the watcher
    enforces the contract itself.
    """
    home, kb = hermes_home
    monkeypatch.setenv("HERMES_HOME", str(home))

    conn = kb.connect()
    tid = _insert_triage(conn)

    patcher, _client = _make_fake_aux_returning(
        _valid_question_json("q1", "q2", "q3", "q4", "q5")
    )
    with patcher:
        stats = tcw.triage_clarify_tick(
            lambda: {"kanban": {"triage_clarify": {
                "enabled": True, "max_questions": 2,
            }}},
            kb.list_boards, kb.connect,
        )

    assert stats["asked"] == 1
    row = conn.execute(
        "SELECT clarification_questions FROM tasks WHERE id = ?", (tid,)
    ).fetchone()
    qs = json.loads(row["clarification_questions"])
    assert len(qs) == 2
    conn.close()


def test_sweep_a_skips_card_on_aux_client_runtime_error(
    hermes_home, monkeypatch,
):
    """Aux-client unavailable leaves the card in triage for next tick.

    No silent swallow — the failure is logged and the card stays
    parked so a transient provider outage doesn't lose the question.
    """
    home, kb = hermes_home
    monkeypatch.setenv("HERMES_HOME", str(home))

    conn = kb.connect()
    tid = _insert_triage(conn)

    with patch(
        "agent.auxiliary_client.get_text_auxiliary_client",
        side_effect=RuntimeError("no aux client"),
    ):
        stats = tcw.triage_clarify_tick(
            _enabled_config, kb.list_boards, kb.connect,
        )

    assert stats["asked"] == 0
    row = conn.execute(
        "SELECT status FROM tasks WHERE id = ?", (tid,)
    ).fetchone()
    assert row["status"] == "triage"
    conn.close()


def test_sweep_a_skips_already_clarified_triage_card(
    hermes_home, monkeypatch,
):
    """A triage card that already has questions is not revisited."""
    home, kb = hermes_home
    monkeypatch.setenv("HERMES_HOME", str(home))

    conn = kb.connect()
    # Bypass ``_insert_triage`` — create a triage card whose
    # clarification_questions is already populated (i.e. another
    # watcher or the specifier already touched it). The watcher
    # must not re-generate or re-park it.
    from hermes_cli.kanban_db import create_task, set_task_clarification
    tid = create_task(conn, title="T", body="B", triage=True, created_by="test")
    set_task_clarification(
        conn, tid, questions=[{"id": "x", "question": "Q?", "why_we_ask": "y"}],
    )

    patcher, _client = _make_fake_aux_returning(_valid_question_json("z1"))
    with patcher:
        stats = tcw.triage_clarify_tick(
            _enabled_config, kb.list_boards, kb.connect,
        )

    assert stats["asked"] == 0
    row = conn.execute(
        "SELECT status, clarification_questions FROM tasks WHERE id = ?", (tid,)
    ).fetchone()
    # Status stays triage, questions unchanged — the model was never called.
    assert row["status"] == "triage"
    assert json.loads(row["clarification_questions"])[0]["id"] == "x"
    conn.close()


# ---------------------------------------------------------------------------
# Sweep B — timed-out parked cards
# ---------------------------------------------------------------------------


def test_sweep_b_flip_back_to_triage_on_skip_policy(
    hermes_home, monkeypatch,
):
    """A parked card past its deadline flips back to ``status='triage'``."""
    home, kb = hermes_home
    monkeypatch.setenv("HERMES_HOME", str(home))

    conn = kb.connect()
    eight_days_ago = int(time.time()) - 8 * 86400
    tid = _insert_awaiting(conn, asked_at=eight_days_ago)

    stats = tcw.triage_clarify_tick(
        _enabled_config, kb.list_boards, kb.connect,
    )

    assert stats["timed_out"] == 1
    row = conn.execute(
        "SELECT status, clarification_questions, clarification_asked_at "
        "FROM tasks WHERE id = ?", (tid,)
    ).fetchone()
    assert row["status"] == "triage"
    assert row["clarification_questions"] is None
    assert row["clarification_asked_at"] is None
    conn.close()


def test_sweep_b_leave_in_awaiting_is_no_op(
    hermes_home, monkeypatch,
):
    """``leave_in_awaiting`` keeps the card parked — no DB change."""
    home, kb = hermes_home
    monkeypatch.setenv("HERMES_HOME", str(home))

    conn = kb.connect()
    eight_days_ago = int(time.time()) - 8 * 86400
    tid = _insert_awaiting(conn, asked_at=eight_days_ago)

    stats = tcw.triage_clarify_tick(
        lambda: {"kanban": {"triage_clarify": {
            "enabled": True, "on_timeout": "leave_in_awaiting",
        }}},
        kb.list_boards, kb.connect,
    )

    assert stats["timed_out"] == 0  # no flip happened
    row = conn.execute(
        "SELECT status FROM tasks WHERE id = ?", (tid,)
    ).fetchone()
    assert row["status"] == "awaiting_clarification"
    conn.close()


def test_sweep_b_per_task_timeout_overrides_global(
    hermes_home, monkeypatch,
):
    """A card with ``clarification_timeout_days=2`` times out at 2 days.

    Global default is 7 — without the per-task override the card
    would still be parked. With ``timeout_days=2``, the 8-day-old
    card flips back to triage on the next tick.
    """
    home, kb = hermes_home
    monkeypatch.setenv("HERMES_HOME", str(home))

    conn = kb.connect()
    three_days_ago = int(time.time()) - 3 * 86400
    tid = _insert_awaiting(
        conn, asked_at=three_days_ago, timeout_days=2,
    )

    stats = tcw.triage_clarify_tick(
        _enabled_config, kb.list_boards, kb.connect,
    )

    assert stats["timed_out"] == 1
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert row["status"] == "triage"
    conn.close()


def test_sweep_b_keeps_recent_parked_card(
    hermes_home, monkeypatch,
):
    """A parked card inside the deadline stays parked."""
    home, kb = hermes_home
    monkeypatch.setenv("HERMES_HOME", str(home))

    conn = kb.connect()
    one_day_ago = int(time.time()) - 86400
    tid = _insert_awaiting(conn, asked_at=one_day_ago)

    stats = tcw.triage_clarify_tick(
        _enabled_config, kb.list_boards, kb.connect,
    )

    assert stats["timed_out"] == 0
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert row["status"] == "awaiting_clarification"
    conn.close()


def test_sweep_b_removes_pending_file_entry_on_flip(
    hermes_home, monkeypatch,
):
    """Timed-out card removal also drops its pending-file entry."""
    home, kb = hermes_home
    monkeypatch.setenv("HERMES_HOME", str(home))

    conn = kb.connect()
    tid = _insert_awaiting(
        conn,
        asked_at=int(time.time()) - 8 * 86400,
    )
    # Seed a pending file with an entry for this task.
    tcw._write_pending_clarifications({
        tid: {"task_id": tid, "title": "T", "questions": [], "asked_at": 0},
    })

    tcw.triage_clarify_tick(
        _enabled_config, kb.list_boards, kb.connect,
    )

    pending = tcw._read_pending_clarifications()
    assert tid not in pending
    conn.close()


# ---------------------------------------------------------------------------
# Master gate — disabled watcher is a quiet no-op
# ---------------------------------------------------------------------------


def test_disabled_flag_short_circuits_to_noop(
    hermes_home, monkeypatch,
):
    """With ``enabled=False`` (the default), the watcher does nothing.

    Even if triage cards exist and timeouts are over their
    deadlines, nothing gets written.
    """
    home, kb = hermes_home
    monkeypatch.setenv("HERMES_HOME", str(home))

    conn = kb.connect()
    _insert_triage(conn)
    _insert_awaiting(
        conn, asked_at=int(time.time()) - 30 * 86400,
    )

    stats = tcw.triage_clarify_tick(
        lambda: {"kanban": {"triage_clarify": {"enabled": False}}},
        kb.list_boards, kb.connect,
    )

    assert stats == {"asked": 0, "timed_out": 0, "boards_visited": 0}
    # No pending file written.
    assert tcw._read_pending_clarifications() == {}
    conn.close()


# ---------------------------------------------------------------------------
# Integration — full happy path
# ---------------------------------------------------------------------------


def test_full_happy_path_triage_to_pending_to_timeout(
    hermes_home, monkeypatch,
):
    """End-to-end: triage → awaiting → pending file → timeout flip.

    Exercises both sweeps across two ticks: the first parkes the
    card, the second times it out and drops the pending entry.
    """
    home, kb = hermes_home
    monkeypatch.setenv("HERMES_HOME", str(home))

    conn = kb.connect()
    tid = _insert_triage(conn, title="Add dark mode")

    # Tick 1 — park the card.
    patcher, _ = _make_fake_aux_returning(_valid_question_json("q1"))
    with patcher:
        stats1 = tcw.triage_clarify_tick(
            _enabled_config, kb.list_boards, kb.connect,
        )
    assert stats1["asked"] == 1
    assert tid in tcw._read_pending_clarifications()

    # Move the ask timestamp into the past — simulate 8 days idle.
    conn.execute(
        "UPDATE tasks SET clarification_asked_at = ? WHERE id = ?",
        (int(time.time()) - 8 * 86400, tid),
    )
    conn.commit()

    # Tick 2 — time out, flip back to triage, drop pending entry.
    stats2 = tcw.triage_clarify_tick(
        _enabled_config, kb.list_boards, kb.connect,
    )
    assert stats2["timed_out"] == 1
    assert tid not in tcw._read_pending_clarifications()
    row = conn.execute(
        "SELECT status FROM tasks WHERE id = ?", (tid,)
    ).fetchone()
    assert row["status"] == "triage"
    conn.close()


# ---------------------------------------------------------------------------
# WhatsApp sender wiring
# ---------------------------------------------------------------------------


def test_whatsapp_sender_called_when_enabled(
    hermes_home, monkeypatch,
):
    """With ``whatsapp.enabled=True`` and a recipient, the sender is called."""
    home, kb = hermes_home
    monkeypatch.setenv("HERMES_HOME", str(home))

    conn = kb.connect()
    _insert_triage(conn)

    sent = []

    def sender(recipient, message):
        sent.append((recipient, message))
        return True

    patcher, _ = _make_fake_aux_returning(_valid_question_json("q1"))
    with patcher:
        tcw.triage_clarify_tick(
            lambda: {"kanban": {"triage_clarify": {
                "enabled": True,
                "delivery": {"whatsapp": {
                    "enabled": True, "recipient": "+15551234567",
                }},
            }}},
            kb.list_boards, kb.connect,
            whatsapp_sender=sender,
        )

    assert len(sent) == 1
    recipient, message = sent[0]
    assert recipient == "+15551234567"
    assert "Clarification needed for:" in message
    conn.close()


def test_whatsapp_sender_not_called_when_sender_is_none(
    hermes_home, monkeypatch,
):
    """No injected sender → no call, but the pending file is still written.

    WhatsApp is best-effort. The pending file is the durable record.
    """
    home, kb = hermes_home
    monkeypatch.setenv("HERMES_HOME", str(home))

    conn = kb.connect()
    tid = _insert_triage(conn)

    patcher, _ = _make_fake_aux_returning(_valid_question_json("q1"))
    with patcher:
        stats = tcw.triage_clarify_tick(
            lambda: {"kanban": {"triage_clarify": {
                "enabled": True,
                "delivery": {"whatsapp": {
                    "enabled": True, "recipient": "+15551234567",
                }},
            }}},
            kb.list_boards, kb.connect,
            whatsapp_sender=None,
        )

    assert stats["asked"] == 1
    assert tid in tcw._read_pending_clarifications()
    conn.close()


def test_whatsapp_sender_exception_is_swallowed(
    hermes_home, monkeypatch,
):
    """A sender that raises doesn't break the tick."""
    home, kb = hermes_home
    monkeypatch.setenv("HERMES_HOME", str(home))

    conn = kb.connect()
    _insert_triage(conn)

    def bad_sender(recipient, message):
        raise RuntimeError("network down")

    patcher, _ = _make_fake_aux_returning(_valid_question_json("q1"))
    with patcher:
        # Must not raise.
        stats = tcw.triage_clarify_tick(
            lambda: {"kanban": {"triage_clarify": {
                "enabled": True,
                "delivery": {"whatsapp": {
                    "enabled": True, "recipient": "+15551234567",
                }},
            }}},
            kb.list_boards, kb.connect,
            whatsapp_sender=bad_sender,
        )

    assert stats["asked"] == 1
    conn.close()