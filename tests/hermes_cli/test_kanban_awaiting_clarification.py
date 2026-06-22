"""Tests for the ``awaiting_clarification`` kanban status + triage-clarify
config block (task t_a121beda).

This is the data-layer foundation for the interactive triage feature: the
specifier parks a task in ``status=awaiting_clarification`` while it waits
for the operator to answer clarification questions, then promotes it back
to ``triage`` with the answers folded into the spec body.

These tests cover the four acceptance bullets from the task spec:

  1. ``VALID_STATUSES`` includes ``awaiting_clarification``; legacy DBs
     still load (the additive migration backfills the four new columns).
  2. The four new columns (``clarification_questions``,
     ``clarification_answers``, ``clarification_asked_at``,
     ``clarification_timeout_days``) round-trip through ``create_task``
     + ``get_task`` via the ``set_task_clarification`` helper.
  3. ``set_task_clarification`` is a partial-update helper: omitting a
     kwarg leaves the existing column untouched, and ``clear_task_clarification``
     resets all four back to NULL.
  4. The ``kanban.triage_clarify`` config block in ``DEFAULT_CONFIG``
     has the documented shape and ``get_triage_clarify_config()`` merges
     user overrides with defaults correctly.

Auxiliary edges:

  - ``awaiting_clarification`` is in ``DISPATCHER_SKIPPED_STATUSES`` so
    the dispatcher can never claim it; the specifier/decomposer already
    filter on ``status='triage'`` so they never visit it either.
  - ``VALID_INITIAL_STATUSES`` includes the new status, so
    ``create_task(initial_status="awaiting_clarification")`` works.
  - Malformed JSON in the new columns is tolerated (returns ``None``)
    so a corrupt row never wedges the dispatcher.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

import pytest

from hermes_cli import kanban as kb_cli
from hermes_cli import kanban_db as kb
from hermes_cli import config as config_mod


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_kanban_home(monkeypatch):
    """Spin up a fresh HERMES_HOME with a clean kanban DB.

    Mirrors the pattern from ``test_kanban_ideas_column.py`` so the
    hermes_cli* module snapshot/restore is correct — without it, the
    reimported ``kanban_db`` keeps its temp HERMES_HOME binding and
    downstream tests in the same session read the wrong DB.
    """
    test_home = tempfile.mkdtemp(prefix="kanban_clarify_test_")
    monkeypatch.setenv("HERMES_HOME", test_home)
    prefixes = ("hermes_cli", "hermes_state")
    snapshot: dict[str, object] = {
        name: mod
        for name, mod in list(sys.modules.items())
        if any(name == p or name.startswith(p + ".") for p in prefixes)
        or name == "hermes_constants"
    }
    for name in list(snapshot):
        del sys.modules[name]
    from hermes_cli import kanban_db  # re-import under the new HOME

    try:
        yield kanban_db, test_home
    finally:
        reimported = [
            name for name in list(sys.modules)
            if any(name == p or name.startswith(p + ".") for p in prefixes)
            or name == "hermes_constants"
        ]
        for name in reimported:
            del sys.modules[name]
        for name, mod in snapshot.items():
            sys.modules.setdefault(name, mod)


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Lightweight HERMES_HOME fixture (no module reimport) for tests that
    only touch the DB layer."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# 1. VALID_STATUSES / old-board compatibility
# ---------------------------------------------------------------------------


def test_awaiting_clarification_in_valid_statuses():
    """The new status must be a valid value for any column that
    checks ``status in VALID_STATUSES`` (claim gate, list_tasks
    filters, status-transition guards, etc.).
    """
    assert "awaiting_clarification" in kb.VALID_STATUSES
    # ``VALID_STATUSES`` is a set, so positional order is
    # non-deterministic; the meaningful contract is membership. The
    # "sits between scheduled and ready" intent is captured in the
    # comment on the constant itself.


def test_awaiting_clarification_in_dispatcher_skipped_statuses():
    """The dispatcher must NEVER touch ``awaiting_clarification`` — the
    status is the specifier's way of saying "human, please respond";
    auto-pipelines are not allowed to claim the card.
    """
    assert "awaiting_clarification" in kb.DISPATCHER_SKIPPED_STATUSES


def test_awaiting_clarification_in_valid_initial_statuses():
    """``create_task(initial_status=...)`` accepts only members of
    ``VALID_INITIAL_STATUSES``. Adding the new status here is what
    lets ``kanban create --initial-status=awaiting_clarification``
    round-trip.
    """
    assert "awaiting_clarification" in kb.VALID_INITIAL_STATUSES


def test_legacy_db_with_only_old_columns_loads_and_clarification_columns_default_to_none(
    tmp_path, monkeypatch,
):
    """Open a DB whose ``tasks`` table is missing the four new columns
    and confirm:

      a) the additive migration backfills them without crashing;
      b) ``Task.from_row`` returns ``None`` for each new field on a
         legacy row (no spurious KeyError).

    This is the old-board compatibility guarantee: every existing
    board must keep working without an export/import.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Build a stripped-down ``tasks`` table by hand (no clarification
    # columns) so the migration path is actually exercised. The
    # default board's DB lives at ``<root>/kanban.db`` (not under
    # ``boards/default/``) — see ``kanban_db_path`` for the resolution
    # chain.
    import sqlite3

    db_path = home / "kanban.db"
    conn = sqlite3.connect(str(db_path))
    # The minimum columns needed for ``create_task`` + ``get_task`` to
    # work. Mirrors the SCHEMA_SQL block in kanban_db.py.
    conn.executescript(
        """
        CREATE TABLE tasks (
            id              TEXT PRIMARY KEY,
            title           TEXT NOT NULL,
            body            TEXT,
            assignee        TEXT,
            status          TEXT NOT NULL,
            priority        INTEGER DEFAULT 0,
            created_by      TEXT,
            created_at      INTEGER NOT NULL,
            started_at      INTEGER,
            completed_at    INTEGER,
            workspace_kind  TEXT NOT NULL DEFAULT 'scratch',
            workspace_path  TEXT,
            branch_name     TEXT,
            claim_lock      TEXT,
            claim_expires   INTEGER,
            tenant          TEXT,
            result          TEXT,
            idempotency_key TEXT,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            worker_pid      INTEGER,
            last_failure_error   TEXT,
            max_runtime_seconds  INTEGER,
            last_heartbeat_at    INTEGER,
            current_run_id       INTEGER,
            workflow_template_id TEXT,
            current_step_key     TEXT,
            skills               TEXT,
            model_override       TEXT,
            max_retries          INTEGER,
            goal_mode            INTEGER NOT NULL DEFAULT 0,
            goal_max_turns       INTEGER,
            session_id           TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at) VALUES (?, ?, ?, ?)",
        ("legacy_1", "old task", "ready", int(time.time())),
    )
    conn.commit()
    conn.close()
    # Init the schema. This must run the additive migration cleanly.
    kb.init_db()
    with kb.connect() as conn:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
        assert "clarification_questions" in cols
        assert "clarification_answers" in cols
        assert "clarification_asked_at" in cols
        assert "clarification_timeout_days" in cols
        task = kb.get_task(conn, "legacy_1")
    assert task is not None
    # All four clarification fields default to None on a row that
    # was inserted before the columns existed.
    assert task.clarification_questions is None
    assert task.clarification_answers is None
    assert task.clarification_asked_at is None
    assert task.clarification_timeout_days is None


# ---------------------------------------------------------------------------
# 2. Column round-trip via set_task_clarification / clear_task_clarification
# ---------------------------------------------------------------------------


def test_set_task_clarification_writes_all_four_fields(kanban_home):
    """The acceptance bullet: create a task, set the four new fields,
    round-trip through ``get_task``, read them back correctly.
    """
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="clarify round-trip")
        questions = [
            {"id": "q1", "question": "Which platform?", "why_we_ask": "drives deps"},
            {"id": "q2", "question": "Deadline?", "why_we_ask": "drives priority"},
        ]
        answers = [
            {"id": "q1", "answer": "iOS"},
        ]
        asked_at = int(time.time())
        timeout_days = 14
        ok = kb.set_task_clarification(
            conn,
            tid,
            questions=questions,
            answers=answers,
            asked_at=asked_at,
            timeout_days=timeout_days,
        )
        assert ok is True

        task = kb.get_task(conn, tid)
        # Lists are returned as native Python lists (deserialised from JSON).
        assert task.clarification_questions == questions
        assert task.clarification_answers == answers
        # Timestamps and counts round-trip as int.
        assert task.clarification_asked_at == asked_at
        assert task.clarification_timeout_days == timeout_days


def test_set_task_clarification_is_partial_update(kanban_home):
    """A second call that only sets ``answers`` must leave
    ``questions`` / ``asked_at`` / ``timeout_days`` untouched. This
    is the call shape the answer-submission UI will use.
    """
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="partial update")
        questions = [{"id": "q1", "question": "Q?", "why_we_ask": "W"}]
        asked_at = 1_700_000_000
        timeout_days = 7
        kb.set_task_clarification(
            conn,
            tid,
            questions=questions,
            asked_at=asked_at,
            timeout_days=timeout_days,
        )
        # Now submit the answer — should NOT clobber the question payload.
        answers = [{"id": "q1", "answer": "A"}]
        kb.set_task_clarification(conn, tid, answers=answers)
        task = kb.get_task(conn, tid)
        assert task.clarification_questions == questions
        assert task.clarification_answers == answers
        assert task.clarification_asked_at == asked_at
        assert task.clarification_timeout_days == timeout_days


def test_set_task_clarification_with_no_kwargs_returns_true_for_existing_task(kanban_home):
    """A no-op call (all kwargs default to ``None``) must still report
    True when the task exists. This is the natural shape of an
    answer-submission UI that may have nothing to write.
    """
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="noop")
        assert kb.set_task_clarification(conn, tid) is True


def test_set_task_clarification_returns_false_for_missing_task(kanban_home):
    """A typo in the task id must surface as ``False`` so callers can
    surface "task not found" rather than a silent success.
    """
    with kb.connect() as conn:
        assert kb.set_task_clarification(
            conn, "t_does_not_exist", questions=[{"id": "q1"}]
        ) is False


def test_clear_task_clarification_resets_all_four_fields(kanban_home):
    """When the pipeline promotes a task out of
    ``awaiting_clarification`` back to ``triage``, all four payload
    columns must be NULL so the next cycle starts clean.
    """
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="clear")
        kb.set_task_clarification(
            conn,
            tid,
            questions=[{"id": "q1", "question": "Q", "why_we_ask": "W"}],
            answers=[{"id": "q1", "answer": "A"}],
            asked_at=1_700_000_000,
            timeout_days=3,
        )
        # Sanity: populated before clear.
        task = kb.get_task(conn, tid)
        assert task.clarification_questions is not None
        assert task.clarification_answers is not None
        assert task.clarification_asked_at is not None
        assert task.clarification_timeout_days is not None

        cleared = kb.clear_task_clarification(conn, tid)
        assert cleared is True
        task = kb.get_task(conn, tid)
        assert task.clarification_questions is None
        assert task.clarification_answers is None
        assert task.clarification_asked_at is None
        assert task.clarification_timeout_days is None


def test_clear_task_clarification_returns_false_for_missing_task(kanban_home):
    """Mirror of the set-helper False-for-missing contract."""
    with kb.connect() as conn:
        assert kb.clear_task_clarification(conn, "t_does_not_exist") is False


def test_create_task_initial_status_awaiting_clarification(kanban_home):
    """``create_task`` must accept ``initial_status="awaiting_clarification"``
    so the specifier can park a task at creation time (avoids the
    create-then-update race when the specifier's first act is to
    ask a question).
    """
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="parked at create",
            initial_status="awaiting_clarification",
        )
        task = kb.get_task(conn, tid)
    assert task.status == "awaiting_clarification"


def test_create_task_rejects_unknown_initial_status(kanban_home):
    """Belt-and-suspenders: VALID_INITIAL_STATUSES is the gate; an
    unknown status must raise so a typo doesn't silently park a card
    in a phantom state.
    """
    with kb.connect() as conn:
        with pytest.raises(ValueError, match="initial_status"):
            kb.create_task(conn, title="bad", initial_status="not_a_status")


# ---------------------------------------------------------------------------
# 3. Malformed JSON tolerance
# ---------------------------------------------------------------------------


def test_from_row_tolerates_malformed_clarification_json(kanban_home):
    """If the clarification JSON column is corrupted (manual SQL
    edit, partial write, etc.) ``from_row`` must return ``None``
    rather than raise — the same lenient pattern the dataclass
    already uses for ``skills``.
    """
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="corrupt json")
        # Stuff a non-JSON blob directly into the column. The
        # ``set_task_clarification`` helper always serialises, so we
        # have to drop down to raw SQL to simulate a corrupted row.
        conn.execute(
            "UPDATE tasks SET clarification_questions = ?, "
            "clarification_answers = ? WHERE id = ?",
            ("{not valid json", "[also not valid", tid),
        )
        conn.commit()
        task = kb.get_task(conn, tid)
    # Lenient: both columns fall back to None instead of crashing the
    # dispatcher the next time it iterates the board.
    assert task.clarification_questions is None
    assert task.clarification_answers is None


def test_from_row_tolerates_non_list_clarification_payload(kanban_home):
    """Defensive: if a future write accidentally stores a JSON object
    (or a bare string) in either column, ``from_row`` should treat it
    as ``None`` rather than pass a dict through to the rest of the
    pipeline that expects a list.
    """
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="dict payload")
        conn.execute(
            "UPDATE tasks SET clarification_questions = ? WHERE id = ?",
            (json.dumps({"id": "q1"}), tid),  # object, not list
        )
        conn.commit()
        task = kb.get_task(conn, tid)
    assert task.clarification_questions is None


# ---------------------------------------------------------------------------
# 4. Config: kanban.triage_clarify block + get_triage_clarify_config()
# ---------------------------------------------------------------------------


def test_default_config_has_triage_clarify_block():
    """The documented defaults must live in ``DEFAULT_CONFIG`` so the
    YAML deep-merge fills them in for any user that hasn't overridden
    the block. The shape here is what downstream modules (the
    specifier, the timeout watcher, the dashboard form) rely on.
    """
    block = config_mod.DEFAULT_CONFIG["kanban"]["triage_clarify"]
    assert isinstance(block, dict)
    # Top-level fields.
    assert block["enabled"] is False
    assert block["max_questions"] == 3
    assert block["timeout_days"] == 7
    assert block["on_timeout"] == "skip_to_decompose"
    # Nested delivery block.
    assert isinstance(block["delivery"], dict)
    assert block["delivery"]["whatsapp"]["enabled"] is False
    assert block["delivery"]["whatsapp"]["recipient"] == ""
    assert block["delivery"]["cli"]["enabled"] is True


def test_get_triage_clarify_config_returns_defaults_when_no_overrides(
    tmp_path, monkeypatch,
):
    """When ``~/.hermes/config.yaml`` doesn't override the block, the
    accessor must return the documented defaults.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    cfg = config_mod.get_triage_clarify_config()
    assert cfg["enabled"] is False
    assert cfg["max_questions"] == 3
    assert cfg["timeout_days"] == 7
    assert cfg["on_timeout"] == "skip_to_decompose"
    assert cfg["delivery"]["whatsapp"]["enabled"] is False
    assert cfg["delivery"]["cli"]["enabled"] is True


def test_get_triage_clarify_config_merges_user_overrides(
    tmp_path, monkeypatch,
):
    """A user who sets ``kanban.triage_clarify.enabled: true`` in
    ``~/.hermes/config.yaml`` must see their value, with the rest of
    the block still populated from defaults. The accessor must NOT
    return a half-built dict that drops ``max_questions`` etc. on
    the floor.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    config_path = home / "config.yaml"
    config_path.write_text(
        "kanban:\n"
        "  triage_clarify:\n"
        "    enabled: true\n"
        "    max_questions: 5\n"
        "    on_timeout: leave_in_awaiting\n"
    )
    # Bust the config cache so the new file is read on the next call.
    config_mod._LOAD_CONFIG_CACHE.clear()
    cfg = config_mod.get_triage_clarify_config()
    assert cfg["enabled"] is True
    assert cfg["max_questions"] == 5
    assert cfg["on_timeout"] == "leave_in_awaiting"
    # Defaults preserved.
    assert cfg["timeout_days"] == 7
    assert cfg["delivery"]["whatsapp"]["enabled"] is False
    assert cfg["delivery"]["cli"]["enabled"] is True


def test_get_triage_clarify_config_merges_per_channel_overrides(
    tmp_path, monkeypatch,
):
    """Setting just one channel under ``delivery`` must not wipe the
    other channel's defaults. The dashboard answers-form needs both
    ``whatsapp`` and ``cli`` keys present to render its template
    without KeyErroring.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    config_path = home / "config.yaml"
    config_path.write_text(
        "kanban:\n"
        "  triage_clarify:\n"
        "    delivery:\n"
        "      whatsapp:\n"
        "        enabled: true\n"
        "        recipient: '+155****4567'\n"
    )
    config_mod._LOAD_CONFIG_CACHE.clear()
    cfg = config_mod.get_triage_clarify_config()
    # Override applied.
    assert cfg["delivery"]["whatsapp"]["enabled"] is True
    assert cfg["delivery"]["whatsapp"]["recipient"] == "+155****4567"
    # Other channel preserved from defaults.
    assert cfg["delivery"]["cli"]["enabled"] is True
