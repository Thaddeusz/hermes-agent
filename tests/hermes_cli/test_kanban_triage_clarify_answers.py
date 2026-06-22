"""Tests for the triage clarification answer capture (task t_a6bc4b73).

These cover three layers end-to-end:

  1. ``hermes_cli/kanban_clarify_answers`` — the parser/validator
     module: ``--q`` flags, JSON stdin/file payloads, merge semantics,
     and error paths.
  2. ``kanban_db.fold_clarification_answers`` — the data-layer fold
     helper that the CLI delegates to. Verifies body rendering,
     idempotency, status flip, and ``clarification_questions`` clear.
  3. ``hermes kanban triage --answer`` — the argparse + dispatch surface.
     Verifies flag wiring, error exits, JSON output mode, and the
     end-to-end happy path (CLI → module → DB → status flip).

The aux LLM client is NOT involved — answers are produced by the
operator, the test just exercises the capture path.
"""

from __future__ import annotations

import argparse
import io
import json as jsonlib
import time
from pathlib import Path

import pytest

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_clarify_answers as kca
from hermes_cli import kanban_db as kb


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Spin up a fresh HERMES_HOME with an initialised kanban DB.

    Same lightweight pattern used across the kanban test suite —
    tmp_path as HERMES_HOME, init_db() runs in the fixture so each test
    gets a clean schema.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _park_task(
    conn,
    *,
    title: str = "rough idea",
    body: str = "TODO flesh out",
    questions: list[dict] | None = None,
    status: str = "awaiting_clarification",
) -> str:
    """Create a task in ``awaiting_clarification`` with a question payload.

    Mirrors what the specifier does in production — write the question
    list into ``clarification_questions``, set ``clarification_asked_at``,
    and leave the body untouched. The fold helper's job is to take this
    state back to ``status='triage'`` with the answers folded in.
    """
    questions = questions if questions is not None else [
        {"id": "q1", "question": "What is the goal?", "why_we_ask": "scope"},
        {"id": "q2", "question": "Who is the audience?", "why_we_ask": "tone"},
    ]
    tid = kb.create_task(conn, title=title, body=body, triage=False)
    # create_task with triage=False lands in 'todo'; we need the parked
    # status. Flip directly so the test isn't coupled to the create
    # helper's status policy.
    now = int(time.time())
    conn.execute(
        "UPDATE tasks SET status = ?, clarification_questions = ?, "
        "clarification_asked_at = ? WHERE id = ?",
        (status, jsonlib.dumps(questions), now, tid),
    )
    conn.commit()
    return tid


def _repark_task(conn, tid: str, *, questions: list[dict] | None = None) -> None:
    """Re-park an already-folded task in awaiting_clarification.

    Simulates a fresh clarification cycle for the idempotency tests —
    the fold helper flips status to ``triage`` on its first call, so
    a second fold needs the task back in the parked state. Same shape
    as :func:`_park_task` minus the create.
    """
    questions = questions if questions is not None else [
        {"id": "q1", "question": "What is the goal?", "why_we_ask": "scope"},
        {"id": "q2", "question": "Who is the audience?", "why_we_ask": "tone"},
    ]
    now = int(time.time())
    conn.execute(
        "UPDATE tasks SET status = ?, clarification_questions = ?, "
        "clarification_asked_at = ? WHERE id = ?",
        ("awaiting_clarification", jsonlib.dumps(questions), now, tid),
    )
    conn.commit()


def _run_cli(*argv: str) -> int:
    """Invoke ``hermes kanban triage ...`` through the real argparse surface."""
    root = argparse.ArgumentParser()
    subp = root.add_subparsers(dest="cmd")
    kanban_cli.build_parser(subp)
    ns = root.parse_args(["kanban", *argv])
    return kanban_cli.kanban_command(ns)


# ---------------------------------------------------------------------------
# 1. Parser / validator — kanban_clarify_answers
# ---------------------------------------------------------------------------


class TestParseQFlag:
    def test_happy_path(self):
        assert kca._parse_q_flag("q1=hello world") == {
            "id": "q1", "answer": "hello world",
        }

    def test_empty_answer_allowed(self):
        """Operators sometimes want to explicitly skip a question; an
        empty answer is a valid signal, not a malformed flag."""
        assert kca._parse_q_flag("q1=") == {"id": "q1", "answer": ""}

    def test_id_may_contain_dots_dashes(self):
        assert kca._parse_q_flag("scope.q-1=ok") == {
            "id": "scope.q-1", "answer": "ok",
        }

    def test_missing_equals_rejected(self):
        with pytest.raises(kca.ClarifyAnswerParseError, match="expected '<id>=<answer>'"):
            kca._parse_q_flag("q1 hello")

    def test_empty_id_rejected(self):
        """An id that's empty (just '=' with no id side) is caught by
        the regex before the explicit check — still a parse error, but
        with the 'expected id=answer' message rather than 'id side is empty'."""
        with pytest.raises(kca.ClarifyAnswerParseError, match="expected '<id>=<answer>'"):
            kca._parse_q_flag("=answer")

    def test_newline_in_answer_rejected(self):
        """Multi-line answers belong in --answer-file / --stdin so the
        CLI stays predictable; a single --q flag is one line."""
        with pytest.raises(kca.ClarifyAnswerParseError, match="contains a newline"):
            kca._parse_q_flag("q1=line1\nline2")


class TestParseQFlags:
    def test_multiple_flags(self):
        out = kca._parse_q_flags(["q1=foo", "q2=bar"])
        assert out == [{"id": "q1", "answer": "foo"}, {"id": "q2", "answer": "bar"}]

    def test_empty_list(self):
        assert kca._parse_q_flags([]) == []

    def test_index_in_error(self):
        """A typo on the third --q should be flagged as such."""
        with pytest.raises(kca.ClarifyAnswerParseError, match="--q\\[2\\]"):
            kca._parse_q_flags(["q1=ok", "q2=ok", "badformat"])


class TestParseJsonPayload:
    def test_canonical_array(self):
        raw = jsonlib.dumps([{"id": "q1", "answer": "a"}, {"id": "q2", "answer": "b"}])
        out = kca._parse_json_payload(raw)
        assert out == [{"id": "q1", "answer": "a"}, {"id": "q2", "answer": "b"}]

    def test_wrapped_answers_key(self):
        raw = jsonlib.dumps({"answers": [{"id": "q1", "answer": "a"}]})
        out = kca._parse_json_payload(raw, source="stdin")
        assert out == [{"id": "q1", "answer": "a"}]

    def test_invalid_json_raises_with_line_col(self):
        with pytest.raises(kca.ClarifyAnswerParseError, match="invalid JSON"):
            kca._parse_json_payload("[{bad", source="stdin")

    def test_non_array_raises(self):
        with pytest.raises(kca.ClarifyAnswerParseError, match="expected a JSON array"):
            kca._parse_json_payload('{"q1": "a"}', source="file")

    def test_empty_array_rejected(self):
        """The fold helper also rejects empty lists; the parser should
        fail-fast with the same intent rather than letting it through."""
        with pytest.raises(kca.ClarifyAnswerParseError, match="at least one answer"):
            kca._parse_json_payload("[]")

    def test_malformed_entry_rejected(self):
        raw = jsonlib.dumps([{"id": "q1"}, {"id": "q2", "answer": "ok"}])
        # Entry 0 has no 'answer' — must be rejected with a precise index.
        with pytest.raises(kca.ClarifyAnswerParseError, match="\\[0\\].answer"):
            kca._parse_json_payload(raw)

    def test_source_in_error_messages(self):
        """When --answer-file fails, the error tells the user which file."""
        with pytest.raises(kca.ClarifyAnswerParseError, match="--answer-file"):
            kca._parse_json_payload("not json", source="--answer-file /tmp/x.json")


class TestParseStdinJson:
    def test_happy_path(self):
        stream = io.StringIO(jsonlib.dumps([{"id": "q1", "answer": "ok"}]))
        assert kca._parse_stdin_json(stream) == [{"id": "q1", "answer": "ok"}]

    def test_empty_stream_rejected(self):
        """A forgotten pipe shouldn't silently submit zero answers."""
        stream = io.StringIO("")
        with pytest.raises(kca.ClarifyAnswerParseError, match="stdin: empty input"):
            kca._parse_stdin_json(stream)

    def test_whitespace_only_stream_rejected(self):
        stream = io.StringIO("   \n  ")
        with pytest.raises(kca.ClarifyAnswerParseError, match="stdin: empty input"):
            kca._parse_stdin_json(stream)


class TestParseAnswerFile:
    def test_happy_path(self, tmp_path):
        p = tmp_path / "answers.json"
        p.write_text(jsonlib.dumps([{"id": "q1", "answer": "from file"}]))
        assert kca._parse_answer_file(str(p)) == [{"id": "q1", "answer": "from file"}]

    def test_missing_file_rejected(self, tmp_path):
        p = tmp_path / "does_not_exist.json"
        with pytest.raises(kca.ClarifyAnswerParseError, match="cannot read"):
            kca._parse_answer_file(str(p))

    def test_empty_file_rejected(self, tmp_path):
        p = tmp_path / "empty.json"
        p.write_text("")
        with pytest.raises(kca.ClarifyAnswerParseError, match="file is empty"):
            kca._parse_answer_file(str(p))


class TestMergeAnswerSources:
    def test_single_source(self):
        out = kca._merge_answer_sources([{"id": "q1", "answer": "a"}])
        assert out == [{"id": "q1", "answer": "a"}]

    def test_multiple_sources_last_wins_per_id(self):
        """Defaults from file, override on the command line — last write wins."""
        file_batch = [{"id": "q1", "answer": "default"}, {"id": "q2", "answer": "stay"}]
        q_batch = [{"id": "q1", "answer": "override"}]
        out = kca._merge_answer_sources(file_batch, q_batch)
        # q1 was overridden, q2 untouched, q1 keeps its (later) position
        # because we mutate in place — but merge_answer_sources preserves
        # first-seen order, only the answer body changes.
        ids = [e["id"] for e in out]
        assert ids == ["q1", "q2"]
        by_id = {e["id"]: e["answer"] for e in out}
        assert by_id == {"q1": "override", "q2": "stay"}

    def test_no_sources_returns_empty(self):
        """Defensive: a misconfigured caller passing zero batches should
        see an empty list rather than crashing. The DB layer rejects the
        empty list with its own error."""
        assert kca._merge_answer_sources() == []


# ---------------------------------------------------------------------------
# 2. Data layer — fold_clarification_answers
# ---------------------------------------------------------------------------


class TestFoldClarificationAnswers:
    def test_happy_path_flips_status_and_appends_section(self, kanban_home):
        with kb.connect() as conn:
            tid = _park_task(conn)

        with kb.connect() as conn:
            new_body = kb.fold_clarification_answers(
                conn, tid,
                answers=[{"id": "q1", "answer": "ship it"}, {"id": "q2", "answer": "devs"}],
                author="ace",
            )
            row = kb.get_task(conn, tid)
            answers_col = row.clarification_answers
            questions_col = row.clarification_questions
            asked_at = row.clarification_asked_at
            body = row.body

        # Status flipped back to triage.
        assert row.status == "triage"
        # Question payload cleared so the watcher doesn't re-ask.
        assert questions_col is None
        assert asked_at is None
        # Audit log carries both sides (question shape + answer).
        assert answers_col[0]["id"] == "q1"
        assert answers_col[0]["answer"] == "ship it"
        assert answers_col[0]["question"] == "What is the goal?"
        # Body now contains a ## User clarifications section.
        assert new_body is not None
        assert "## User clarifications" in body
        assert "ship it" in body
        assert "What is the goal?" in body  # original question rendered inline

    def test_unknown_task_returns_none(self, kanban_home):
        with kb.connect() as conn:
            result = kb.fold_clarification_answers(
                conn, "t_does_not_exist",
                answers=[{"id": "q1", "answer": "x"}],
            )
        assert result is None

    def test_non_parked_task_returns_none(self, kanban_home):
        """Folding into a task that's not in awaiting_clarification must
        NOT silently write answers — the caller would lose them."""
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="not parked", body="x", triage=True)
        with kb.connect() as conn:
            result = kb.fold_clarification_answers(
                conn, tid,
                answers=[{"id": "q1", "answer": "x"}],
            )
        assert result is None
        with kb.connect() as conn:
            row = kb.get_task(conn, tid)
        # Body untouched.
        assert row.body == "x"
        assert row.status == "triage"  # still in triage, never parked

    def test_empty_answers_raises_value_error(self, kanban_home):
        with kb.connect() as conn:
            tid = _park_task(conn)
        with kb.connect() as conn:
            with pytest.raises(ValueError, match="non-empty list"):
                kb.fold_clarification_answers(conn, tid, answers=[])

    def test_malformed_answer_raises_value_error(self, kanban_home):
        with kb.connect() as conn:
            tid = _park_task(conn)
        with kb.connect() as conn:
            with pytest.raises(ValueError, match="must be a string"):
                kb.fold_clarification_answers(
                    conn, tid,
                    answers=[{"id": "q1", "answer": 42}],
                )

    def test_idempotent_refold_produces_byte_identical_body(self, kanban_home):
        """After the first fold the task is back in triage. Re-park it
        (simulating a fresh clarification cycle) and re-fold with the
        same answers; the body must be byte-identical so re-folds never
        duplicate the section."""
        with kb.connect() as conn:
            tid = _park_task(conn)
        with kb.connect() as conn:
            first = kb.fold_clarification_answers(
                conn, tid,
                answers=[{"id": "q1", "answer": "x"}, {"id": "q2", "answer": "y"}],
            )
        with kb.connect() as conn:
            _repark_task(conn, tid)
        with kb.connect() as conn:
            second = kb.fold_clarification_answers(
                conn, tid,
                answers=[{"id": "q1", "answer": "x"}, {"id": "q2", "answer": "y"}],
            )
        assert first == second
        # The section heading appears exactly once.
        assert first.count("## User clarifications") == 1

    def test_refold_with_different_answers_replaces_section(self, kanban_home):
        """Changing an answer on a re-parked task should replace (not
        duplicate) the section."""
        with kb.connect() as conn:
            tid = _park_task(conn)
        with kb.connect() as conn:
            first = kb.fold_clarification_answers(
                conn, tid,
                answers=[{"id": "q1", "answer": "old"}, {"id": "q2", "answer": "y"}],
            )
        with kb.connect() as conn:
            _repark_task(conn, tid)
        with kb.connect() as conn:
            second = kb.fold_clarification_answers(
                conn, tid,
                answers=[{"id": "q1", "answer": "new"}, {"id": "q2", "answer": "y"}],
            )
        assert first != second
        assert second.count("## User clarifications") == 1
        assert "new" in second
        assert "old" not in second

    def test_audit_comment_recorded_with_author(self, kanban_home):
        with kb.connect() as conn:
            tid = _park_task(conn)
        with kb.connect() as conn:
            kb.fold_clarification_answers(
                conn, tid,
                answers=[{"id": "q1", "answer": "x"}],
                author="alice",
            )
        with kb.connect() as conn:
            comments = kb.list_comments(conn, tid)
        assert any("CLARIFICATION_ANSWERS" in c.body for c in comments)
        assert any(c.author == "alice" for c in comments)

    def test_no_audit_comment_when_author_omitted(self, kanban_home):
        """Programmatic callers (e.g. future webhook handler) should be
        able to skip the audit comment."""
        with kb.connect() as conn:
            tid = _park_task(conn)
        with kb.connect() as conn:
            kb.fold_clarification_answers(
                conn, tid,
                answers=[{"id": "q1", "answer": "x"}],
            )
        with kb.connect() as conn:
            comments = kb.list_comments(conn, tid)
        assert not any("CLARIFICATION_ANSWERS" in c.body for c in comments)

    def test_unanswered_question_listed_with_no_answer_marker(self, kanban_home):
        """If the operator didn't answer every question, the gap should
        be visible in the audit payload (not silently dropped)."""
        with kb.connect() as conn:
            tid = _park_task(conn)
        with kb.connect() as conn:
            kb.fold_clarification_answers(
                conn, tid,
                answers=[{"id": "q1", "answer": "only one"}],
            )
            answers_col = kb.get_task(conn, tid).clarification_answers
        by_id = {a["id"]: a for a in answers_col}
        assert by_id["q1"]["answer"] == "only one"
        assert by_id["q2"]["answer"] is None
        assert by_id["q2"]["question"] == "Who is the audience?"

    def test_extra_answer_id_preserved_at_end(self, kanban_home):
        """Operator may provide answers for ids not in the question
        payload (free-form context); they should land in the audit log
        rather than be silently dropped."""
        with kb.connect() as conn:
            tid = _park_task(conn)
        with kb.connect() as conn:
            kb.fold_clarification_answers(
                conn, tid,
                answers=[
                    {"id": "q1", "answer": "a"},
                    {"id": "q2", "answer": "b"},
                    {"id": "extra", "answer": "freeform context"},
                ],
            )
            answers_col = kb.get_task(conn, tid).clarification_answers
        ids = [a["id"] for a in answers_col]
        # Question-order answers first, then extras.
        assert ids == ["q1", "q2", "extra"]
        assert answers_col[-1]["answer"] == "freeform context"

    def test_task_without_question_payload_still_folds(self, kanban_home):
        """Defensive: specifier parked the card without writing
        clarification_questions (shouldn't happen in practice). The fold
        should still flip the status and write the answers."""
        with kb.connect() as conn:
            tid = _park_task(conn, questions=[])  # no payload
        with kb.connect() as conn:
            new_body = kb.fold_clarification_answers(
                conn, tid,
                answers=[{"id": "q1", "answer": "x"}],
            )
            row = kb.get_task(conn, tid)
        assert row.status == "triage"
        assert new_body is not None
        assert "## User clarifications" in row.body


# ---------------------------------------------------------------------------
# 3. CLI surface — hermes kanban triage --answer
# ---------------------------------------------------------------------------


class TestCmdTriage:
    def test_requires_answer_task_id(self, kanban_home, capsys):
        rc = _run_cli("triage", "-q", "q1=foo")
        assert rc == 2
        err = capsys.readouterr().err
        assert "--answer <TASK_ID>" in err

    def test_requires_at_least_one_answer_source(self, kanban_home, capsys):
        rc = _run_cli("triage", "--answer", "t_foo")
        assert rc == 2
        err = capsys.readouterr().err
        assert "at least one of --q, --answer-file, or --stdin" in err

    def test_happy_path_via_q_flag(self, kanban_home, capsys):
        with kb.connect() as conn:
            tid = _park_task(conn)
        rc = _run_cli("triage", "--answer", tid, "-q", "q1=ship it", "-q", "q2=devs")
        assert rc == 0
        out = capsys.readouterr().out
        assert "Folded 2 answer(s)" in out
        assert tid in out
        # DB state actually changed.
        with kb.connect() as conn:
            row = kb.get_task(conn, tid)
        assert row.status == "triage"
        assert row.clarification_questions is None

    def test_happy_path_via_json_output(self, kanban_home, capsys):
        with kb.connect() as conn:
            tid = _park_task(conn)
        rc = _run_cli(
            "triage", "--answer", tid,
            "-q", "q1=a", "-q", "q2=b",
            "--json",
        )
        assert rc == 0
        out = capsys.readouterr().out.strip()
        parsed = jsonlib.loads(out)
        assert parsed["task_id"] == tid
        assert parsed["ok"] is True
        assert parsed["answer_count"] == 2
        assert parsed["status"] == "triage"

    def test_happy_path_via_answer_file(self, kanban_home, tmp_path):
        with kb.connect() as conn:
            tid = _park_task(conn)
        af = tmp_path / "answers.json"
        af.write_text(jsonlib.dumps([
            {"id": "q1", "answer": "from file 1"},
            {"id": "q2", "answer": "from file 2"},
        ]))
        rc = _run_cli("triage", "--answer", tid, "--answer-file", str(af))
        assert rc == 0
        with kb.connect() as conn:
            row = kb.get_task(conn, tid)
            answers = row.clarification_answers
        assert row.status == "triage"
        assert {a["id"] for a in answers} == {"q1", "q2"}
        assert answers[0]["answer"] == "from file 1"

    def test_combined_q_and_answer_file_last_wins(self, kanban_home, tmp_path):
        """File supplies defaults, --q overrides one entry."""
        with kb.connect() as conn:
            tid = _park_task(conn)
        af = tmp_path / "answers.json"
        af.write_text(jsonlib.dumps([
            {"id": "q1", "answer": "default"},
            {"id": "q2", "answer": "stay"},
        ]))
        rc = _run_cli(
            "triage", "--answer", tid,
            "--answer-file", str(af),
            "-q", "q1=override",
        )
        assert rc == 0
        with kb.connect() as conn:
            answers = kb.get_task(conn, tid).clarification_answers
        by_id = {a["id"]: a["answer"] for a in answers}
        assert by_id["q1"] == "override"
        assert by_id["q2"] == "stay"

    def test_unknown_task_exits_1(self, kanban_home, capsys):
        rc = _run_cli(
            "triage", "--answer", "t_does_not_exist",
            "-q", "q1=x",
        )
        assert rc == 1
        err = capsys.readouterr().err
        assert "task not found" in err

    def test_wrong_status_exits_1_with_precise_message(self, kanban_home, capsys):
        """Folding into a non-parked task must surface the actual status,
        not a generic 'nothing happened'."""
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="todo task", body="x", triage=True)
        rc = _run_cli("triage", "--answer", tid, "-q", "q1=x")
        assert rc == 1
        err = capsys.readouterr().err
        assert "task is in status='triage'" in err
        assert "expected 'awaiting_clarification'" in err

    def test_malformed_q_flag_exits_2(self, kanban_home, capsys):
        with kb.connect() as conn:
            tid = _park_task(conn)
        rc = _run_cli("triage", "--answer", tid, "-q", "no_equals_sign")
        assert rc == 2
        err = capsys.readouterr().err
        assert "expected '<id>=<answer>'" in err
        # DB not touched.
        with kb.connect() as conn:
            row = kb.get_task(conn, tid)
        assert row.status == "awaiting_clarification"

    def test_malformed_answer_file_exits_2(self, kanban_home, tmp_path, capsys):
        with kb.connect() as conn:
            tid = _park_task(conn)
        af = tmp_path / "bad.json"
        af.write_text("not json at all")
        rc = _run_cli("triage", "--answer", tid, "--answer-file", str(af))
        assert rc == 2
        err = capsys.readouterr().err
        assert "--answer-file" in err
        assert "invalid JSON" in err
        # DB not touched.
        with kb.connect() as conn:
            row = kb.get_task(conn, tid)
        assert row.status == "awaiting_clarification"

    def test_empty_answer_file_exits_2(self, kanban_home, tmp_path, capsys):
        with kb.connect() as conn:
            tid = _park_task(conn)
        af = tmp_path / "empty.json"
        af.write_text("")
        rc = _run_cli("triage", "--answer", tid, "--answer-file", str(af))
        assert rc == 2
        assert "file is empty" in capsys.readouterr().err

    def test_idempotent_refold_via_cli(self, kanban_home, capsys):
        """After the first CLI fold, the task is in triage. Re-park it
        (simulating a fresh clarification cycle) and run the CLI again
        with the same answers; the second run must also succeed and
        produce a body with a single (non-duplicated) section."""
        with kb.connect() as conn:
            tid = _park_task(conn)
        _run_cli("triage", "--answer", tid, "-q", "q1=x", "-q", "q2=y")
        # Re-park and re-fold.
        with kb.connect() as conn:
            _repark_task(conn, tid)
        capsys.readouterr()  # clear first output
        rc = _run_cli("triage", "--answer", tid, "-q", "q1=x", "-q", "q2=y")
        assert rc == 0
        out = capsys.readouterr().out
        assert "Folded 2 answer(s)" in out
        with kb.connect() as conn:
            body = kb.get_task(conn, tid).body
        assert body.count("## User clarifications") == 1

    def test_no_op_when_answers_unchanged_does_not_double_comment(
        self, kanban_home,
    ):
        """When a re-parked task is folded with answers identical to
        the previous fold, the no-op detection in the data layer must
        skip the audit comment so we don't spam the comment thread."""
        with kb.connect() as conn:
            tid = _park_task(conn)
        _run_cli("triage", "--answer", tid, "-q", "q1=x", "-q", "q2=y", "--author", "bob")
        with kb.connect() as conn:
            _repark_task(conn, tid)
        _run_cli("triage", "--answer", tid, "-q", "q1=x", "-q", "q2=y", "--author", "bob")
        with kb.connect() as conn:
            clar_comments = [
                c for c in kb.list_comments(conn, tid)
                if "CLARIFICATION_ANSWERS" in c.body
            ]
        assert len(clar_comments) == 1

    def test_author_recorded_on_audit_comment(self, kanban_home):
        with kb.connect() as conn:
            tid = _park_task(conn)
        _run_cli(
            "triage", "--answer", tid,
            "-q", "q1=x",
            "--author", "bob",
        )
        with kb.connect() as conn:
            comments = kb.list_comments(conn, tid)
        assert any(c.author == "bob" for c in comments)
        assert any("CLARIFICATION_ANSWERS" in c.body for c in comments)


# ---------------------------------------------------------------------------
# 4. Public entry point — submit_clarification_answers (for future webhook)
# ---------------------------------------------------------------------------


class TestSubmitClarificationAnswers:
    """The future chat-webhook handler should call this entry point
    directly, bypassing the CLI parser. These tests pin the contract."""

    def test_happy_path(self, kanban_home):
        with kb.connect() as conn:
            tid = _park_task(conn)
        new_body = kca.submit_clarification_answers(
            tid,
            answers=[{"id": "q1", "answer": "x"}, {"id": "q2", "answer": "y"}],
            author="webhook",
        )
        assert new_body is not None
        with kb.connect() as conn:
            row = kb.get_task(conn, tid)
        assert row.status == "triage"

    def test_empty_answers_raises_value_error(self, kanban_home):
        with kb.connect() as conn:
            tid = _park_task(conn)
        with pytest.raises(ValueError):
            kca.submit_clarification_answers(tid, answers=[])

    def test_malformed_answer_raises_value_error(self, kanban_home):
        """Even though the parser usually catches this, the public entry
        point must guard against bypass for safety."""
        with kb.connect() as conn:
            tid = _park_task(conn)
        with pytest.raises(ValueError):
            kca.submit_clarification_answers(
                tid, answers=[{"id": "q1"}],  # missing 'answer'
            )

    def test_unknown_task_returns_none(self, kanban_home):
        assert kca.submit_clarification_answers(
            "t_does_not_exist",
            answers=[{"id": "q1", "answer": "x"}],
        ) is None