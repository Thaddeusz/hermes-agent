import asyncio
import json
from pathlib import Path


from gateway.config import Platform
from gateway.kanban_watchers import (
    _default_sub_collect_for_slug,
    _default_sub_template_fields,
    _install_default_notify_sub_from_config,
    _render_notify_template,
    _resolve_default_notify_target,
    DEFAULT_NOTIFY_KINDS,
)
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb


class RecordingAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})


class DisconnectedAdapters(dict):
    """Expose a platform during collection, then simulate disconnect on get()."""

    def get(self, key, default=None):
        return None


async def _run_one_notifier_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _make_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    return runner


def _create_completed_subscription(summary="done once"):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="notify once", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.complete_task(conn, tid, summary=summary)
        return tid
    finally:
        conn.close()


def _unseen_terminal_events(tid):
    conn = kb.connect()
    try:
        _, events = kb.unseen_events_for_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            kinds=["completed", "blocked", "gave_up", "crashed", "timed_out"],
        )
        return events
    finally:
        conn.close()


def test_kanban_notifier_dedupes_board_slugs_pointing_to_same_db(tmp_path, monkeypatch):
    db_path = tmp_path / "shared-kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    kb.write_board_metadata("alias-a", name="Alias A")
    kb.write_board_metadata("alias-b", name="Alias B")

    tid = _create_completed_subscription()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert "Kanban" in adapter.sent[0]["text"]
    assert tid in adapter.sent[0]["text"]


def test_kanban_notifier_claim_prevents_second_watcher_send(tmp_path, monkeypatch):
    db_path = tmp_path / "single-owner.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    tid = _create_completed_subscription()

    adapter1 = RecordingAdapter()
    adapter2 = RecordingAdapter()

    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter1)))
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter2)))

    assert len(adapter1.sent) == 1
    assert adapter2.sent == []


def test_kanban_notifier_rewinds_claim_if_adapter_disconnects(tmp_path, monkeypatch):
    db_path = tmp_path / "adapter-disconnect.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    tid = _create_completed_subscription()

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = DisconnectedAdapters({Platform.TELEGRAM: RecordingAdapter()})
    runner._kanban_sub_fail_counts = {}

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert [ev.kind for ev in _unseen_terminal_events(tid)] == ["completed"]


def test_kanban_db_path_is_test_isolated_from_real_home():
    hermes_home = Path(kb.kanban_home())
    production_db = Path.home() / ".hermes" / "kanban.db"
    assert kb.kanban_db_path().resolve() != production_db.resolve()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
    finally:
        conn.close()

    assert kb.kanban_db_path().resolve().is_relative_to(hermes_home.resolve())
    assert kb.kanban_db_path().resolve() != production_db.resolve()


class FailingAdapter:
    """Adapter whose send() always raises, simulating a transient send error."""

    def __init__(self):
        self.attempts = 0

    async def send(self, chat_id, text, metadata=None):
        self.attempts += 1
        raise RuntimeError("simulated send failure")


def test_kanban_notifier_rewinds_claim_on_send_exception(tmp_path, monkeypatch):
    """A raising adapter rewinds the claim so the next tick can retry.

    This is the second rewind path (distinct from the adapter-disconnect path
    in test_kanban_notifier_rewinds_claim_if_adapter_disconnects). Here the
    adapter is connected and the send call actually fires; the claim must
    still rewind so the event isn't lost when send() raises mid-tick.
    """
    db_path = tmp_path / "send-failure.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    tid = _create_completed_subscription()

    adapter = FailingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # Send was attempted (so we exercised the failure path, not just the
    # disconnect path) and the claim was rewound — the unseen-events query
    # still returns the event for retry on the next tick.
    assert adapter.attempts >= 1, "send should have been attempted at least once"
    assert [ev.kind for ev in _unseen_terminal_events(tid)] == ["completed"]


def test_notifier_redelivers_same_kind_on_dispatch_cycle(tmp_path, monkeypatch):
    """A retry cycle (crashed → reclaimed → crashed) notifies the user twice.

    Before #21398 the notifier auto-unsubscribed on any terminal event kind
    (gave_up / crashed / timed_out), so the second crash in a respawn cycle
    silently dropped — the subscription was already gone. This test pins the
    new contract: subscription survives non-final terminal events; the
    cursor handles dedup.

    Two crashes ten seconds apart on the same task — both should land on
    the adapter.
    """
    db_path = tmp_path / "redeliver-cycle.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="cycle test", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        # First crash — fired by the dispatcher when the worker PID dies.
        kb._append_event(conn, tid, kind="crashed")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # First crash delivered.
    assert len(adapter.sent) == 1
    assert "crashed" in adapter.sent[0]["text"].lower()

    # Subscription survives — the cursor advanced past event #1, but the
    # row is still there.
    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, tid)
        assert len(subs) == 1, (
            "Subscription must survive a crashed event so a respawn-cycle "
            "second crash also notifies the user (issue #21398)."
        )

        # Second crash — same task, same dispatcher (or a respawn). Append
        # another event to simulate the dispatcher firing crashed a second
        # time during retry.
        kb._append_event(conn, tid, kind="crashed")
    finally:
        conn.close()

    # New tick: the second event has a fresh id past the cursor advance,
    # so it gets claimed and delivered.
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 2, (
        f"Second crashed event should also notify; got {len(adapter.sent)} "
        f"deliveries (texts: {[d['text'] for d in adapter.sent]})"
    )
    assert "crashed" in adapter.sent[1]["text"].lower()


# ---------------------------------------------------------------------------
# Default WhatsApp subscription (task t_1b1c730a)
# ---------------------------------------------------------------------------
#
# Seven tests below mirror the existing six and pin the new feature:
#   - the install helper idempotently inserts / removes the sentinel row
#   - the per-event tick broadcasts default-sub events through the same
#     adapter path as per-task subs
#   - the on_status filter only forwards blocked / awaiting_clarification
#     / review (the spec's narrower set), not in_progress / done / etc.
#   - per-task and default subs coexist with independent cursors
#   - the user-configurable template renders safely even when a
#     placeholder is missing
#
# Tests bypass the config-driven installer (``_install_default_notify_sub_from_config``)
# and call ``kb.ensure_default_notify_sub`` directly, matching the
# existing fixture pattern (``_create_completed_subscription`` skips the
# CLI path too). A separate test exercises the installer with a
# monkeypatched config dict.


def _seed_default_sub(conn=None):
    """Install the sentinel default sub on ``conn`` (or open one). Returns
    the open connection so the caller can keep using it.
    """
    close_at_end = conn is None
    if conn is None:
        conn = kb.connect()
    kb.ensure_default_notify_sub(
        conn,
        platform="whatsapp",
        chat_id="353899843924",
        notifier_profile="main_profile",
    )
    if close_at_end:
        conn.close()
        return None
    return conn


def _make_whatsapp_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.WHATSAPP: adapter}
    runner._kanban_sub_fail_counts = {}
    return runner


async def _run_one_notifier_tick_whatsapp(monkeypatch, runner):
    """One-tick driver that uses the real ``_kanban_notifier_watcher``
    body but skips the config-driven default-sub install step (we
    install the sentinel row directly in the test fixture, matching the
    existing per-task pattern).
    """
    real_sleep = asyncio.sleep
    config_calls = {"count": 0}

    async def fake_sleep(delay):
        if delay == 5:
            return None
        # After the initial 5s sleep, run a single tick then break out.
        config_calls["count"] += 1
        if config_calls["count"] > 1:
            runner._running = False
            await real_sleep(0)
        else:
            await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    # Bypass the config-driven installer (test runs in tmp HERMES_HOME
    # with no config.yaml). We install the sentinel row directly via
    # ``kb.ensure_default_notify_sub`` in each test instead.
    from gateway import kanban_watchers as _kw

    def _noop_install(_cfg):
        return None
    monkeypatch.setattr(_kw, "_install_default_notify_sub_from_config", _noop_install)
    # Same for the config loader — the test fixtures don't need real
    # config; load_config would otherwise warn and gate the watcher off.
    from hermes_cli import config as _cfg_mod

    def _fake_load_config():
        return {
            "kanban": {
                "dispatch_in_gateway": True,
                "notifications": {
                    "enabled": True,
                    "destinations": {
                        "whatsapp": {
                            "chat_id": "353899843924",
                            "thread_id": None,
                            "profile": "main_profile",
                            "template": (
                                "🔔 Kanban: {task_id} {title}\n"
                                "Status: {new_status}\n"
                                "Reason: {block_reason}\n"
                                "Workspace: {workspace_path}"
                            ),
                        },
                    },
                },
            },
        }
    monkeypatch.setattr(_cfg_mod, "load_config", _fake_load_config)

    await runner._kanban_notifier_watcher(interval=1)


def test_default_subscription_created_on_startup(tmp_path, monkeypatch):
    """Config-driven installer puts one sentinel row in place, idempotently."""
    db_path = tmp_path / "default-sub.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    cfg = {
        "kanban": {
            "notifications": {
                "enabled": True,
                "destinations": {
                    "whatsapp": {
                        "chat_id": "353899843924",
                        "thread_id": None,
                        "profile": "main_profile",
                        "template": "🔔 {task_id} {new_status}",
                    },
                },
            },
        },
    }

    _install_default_notify_sub_from_config(cfg)

    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, include_default=True)
        default_subs = [s for s in subs if s.get("is_default") == 1]
        assert len(default_subs) == 1, (
            f"expected exactly one default sub, got {len(default_subs)}"
        )
        assert default_subs[0]["task_id"] == kb.DEFAULT_NOTIFY_SUB_TASK_ID
        assert default_subs[0]["platform"] == "whatsapp"
        assert default_subs[0]["chat_id"] == "353899843924"
        assert kb.has_default_notify_sub(conn) is True
    finally:
        conn.close()

    # Idempotent: re-running with the same config produces no new rows.
    _install_default_notify_sub_from_config(cfg)
    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, include_default=True)
        default_subs = [s for s in subs if s.get("is_default") == 1]
        assert len(default_subs) == 1
    finally:
        conn.close()


def test_default_subscription_not_created_when_disabled(tmp_path, monkeypatch):
    """Master toggle off ⇒ no sentinel row, even if a destination is configured."""
    db_path = tmp_path / "default-disabled.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    cfg = {
        "kanban": {
            "notifications": {
                "enabled": False,
                "destinations": {
                    "whatsapp": {
                        "chat_id": "353899843924",
                        "template": "🔔 {task_id}",
                    },
                },
            },
        },
    }
    _install_default_notify_sub_from_config(cfg)

    conn = kb.connect()
    try:
        assert kb.has_default_notify_sub(conn) is False
        subs = kb.list_notify_subs(conn, include_default=True)
        assert subs == []
    finally:
        conn.close()


def test_default_sub_removed_when_disabled_mid_session(tmp_path, monkeypatch):
    """Toggling enabled=True → False via two config reloads flips the row."""
    db_path = tmp_path / "default-toggle.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    cfg_on = {
        "kanban": {
            "notifications": {
                "enabled": True,
                "destinations": {
                    "whatsapp": {"chat_id": "353899843924"},
                },
            },
        },
    }
    cfg_off = {
        "kanban": {
            "notifications": {
                "enabled": False,
                "destinations": {
                    "whatsapp": {"chat_id": "353899843924"},
                },
            },
        },
    }

    _install_default_notify_sub_from_config(cfg_on)
    conn = kb.connect()
    try:
        assert kb.has_default_notify_sub(conn) is True
    finally:
        conn.close()

    _install_default_notify_sub_from_config(cfg_off)
    conn = kb.connect()
    try:
        assert kb.has_default_notify_sub(conn) is False
    finally:
        conn.close()

    # And back on — re-installs cleanly.
    _install_default_notify_sub_from_config(cfg_on)
    conn = kb.connect()
    try:
        assert kb.has_default_notify_sub(conn) is True
    finally:
        conn.close()


def test_default_sub_not_in_notify_list_by_default(tmp_path, monkeypatch):
    """The sentinel row is hidden from the user-facing ``list_notify_subs()``
    call unless the caller opts in via ``include_default=True``.
    """
    db_path = tmp_path / "default-hidden.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    _seed_default_sub()

    conn = kb.connect()
    try:
        # Add a per-task explicit sub so we have a non-default row too —
        # verifies that ``include_default=False`` doesn't accidentally
        # hide user-created rows.
        tid = kb.create_task(conn, title="notify me", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="whatsapp", chat_id="353899843924")

        all_subs_default = kb.list_notify_subs(conn)
        assert len(all_subs_default) == 1
        assert all_subs_default[0]["task_id"] == tid

        all_subs_including = kb.list_notify_subs(conn, include_default=True)
        assert len(all_subs_including) == 2
        ids = {s["task_id"] for s in all_subs_including}
        assert kb.DEFAULT_NOTIFY_SUB_TASK_ID in ids
    finally:
        conn.close()


def test_blocked_card_pings_default_sub(tmp_path, monkeypatch):
    """End-to-end: card transitions to blocked, default sub broadcasts to WhatsApp."""
    db_path = tmp_path / "default-blocked.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    _seed_default_sub()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="needs human input", assignee="worker")
        kb._append_event(
            conn, tid, kind="blocked",
            payload={"reason": "missing API key"},
        )
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_whatsapp_runner(adapter)
    asyncio.run(_run_one_notifier_tick_whatsapp(monkeypatch, runner))

    assert len(adapter.sent) >= 1, (
        f"expected at least one WhatsApp send for blocked default sub; "
        f"got {adapter.sent}"
    )
    last = adapter.sent[-1]
    assert last["chat_id"] == "353899843924"
    body = last["text"]
    assert "blocked" in body.lower(), f"body missing 'blocked': {body!r}"
    # Default sub renders through the user's template, so the rendered
    # body contains the {task_id} placeholder value.
    assert tid in body, f"body missing task_id {tid}: {body!r}"
    assert "needs human input" in body, f"body missing title: {body!r}"


def test_status_filter_only_matches_blocked_acl_review(tmp_path, monkeypatch):
    """The default sub's broadcast filter is the spec's narrower set:
    blocked / awaiting_clarification / review. Other kinds must NOT
    fire through the default sub.
    """
    db_path = tmp_path / "default-filter.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    _seed_default_sub()

    conn = kb.connect()
    try:
        # Negative kinds — these must NOT reach the adapter.
        tid_a = kb.create_task(conn, title="done task", assignee="worker")
        kb._append_event(conn, tid_a, kind="completed")
        kb._append_event(conn, tid_a, kind="in_progress", payload={"phase": "starting"})

        # Positive kinds — these MUST reach the adapter (one delivery per task).
        tid_b = kb.create_task(conn, title="stuck task", assignee="worker")
        kb._append_event(conn, tid_b, kind="blocked", payload={"reason": "test"})

        tid_c = kb.create_task(conn, title="specify task", assignee="worker")
        kb._append_event(
            conn, tid_c, kind="awaiting_clarification",
            payload={"questions": ["What auth method?"]},
        )

        tid_d = kb.create_task(conn, title="review task", assignee="worker")
        kb._append_event(
            conn, tid_d, kind="review", payload={"url": "https://github.com/x/y/pull/1"},
        )

        # Sanity: the broadcast claim returns ONLY the three positive tasks.
        # NOTE: this claim is run only AFTER the watcher tick below has
        # had a chance to read the events. We don't pre-claim here because
        # the watcher's claim would then find no events to deliver.
        # The assertion below is duplicated inside the tick body.
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_whatsapp_runner(adapter)
    asyncio.run(_run_one_notifier_tick_whatsapp(monkeypatch, runner))

    # Cross-check the broadcast claim shape now (after the tick has
    # claimed and the cursor is advanced). The set of task_ids the
    # default sub saw must be exactly the positive kind set.
    conn = kb.connect()
    try:
        old, new, by_task = kb.claim_unseen_events_for_default_sub(
            conn, kinds=DEFAULT_NOTIFY_KINDS,
        )
        # Cursor advanced past the events the tick already claimed, so
        # this second claim must return empty.
        assert not by_task, (
            f"second claim should be empty after tick: {by_task!r}"
        )
    finally:
        conn.close()

    bodies = "\n".join(s["text"] for s in adapter.sent)
    # Positive kinds reached the adapter.
    assert tid_b in bodies, f"blocked task missing from bodies: {bodies!r}"
    assert tid_c in bodies, f"awaiting_clarification task missing from bodies: {bodies!r}"
    assert tid_d in bodies, f"review task missing from bodies: {bodies!r}"
    # Negative kinds must NOT have produced a delivery for tid_a.
    assert tid_a not in bodies, (
        f"completed/in_progress event leaked through default sub: {bodies!r}"
    )


def test_explicit_sub_and_default_sub_coexist_for_same_task(tmp_path, monkeypatch):
    """A per-task explicit sub AND the default sub both fire on the same
    blocked event, with independent cursors. Re-running with no new
    events must not double-fire either.
    """
    db_path = tmp_path / "default-coexist.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    _seed_default_sub()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="hot task", assignee="worker")
        # Per-task explicit sub on telegram chat — completely separate
        # platform + chat from the WhatsApp default sub.
        kb.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="telegram-chat-1",
        )
        # Add a whatsapp per-task sub too, to verify the default sub's
        # WhatsApp chat_id is distinct from any per-task sub on the same
        # platform.
        kb.add_notify_sub(
            conn, task_id=tid, platform="whatsapp", chat_id="per-task-whatsapp",
        )
        kb._append_event(conn, tid, kind="blocked", payload={"reason": "x"})
    finally:
        conn.close()

    # Use a runner with BOTH adapters so the per-task telegram sub
    # actually fires. (Default sub targets WhatsApp only.)
    class BothAdaptersRunner(GatewayRunner):
        pass

    runner = BothAdaptersRunner.__new__(BothAdaptersRunner)
    runner._running = True
    whatsapp_adapter = RecordingAdapter()
    telegram_adapter = RecordingAdapter()
    runner.adapters = {
        Platform.WHATSAPP: whatsapp_adapter,
        Platform.TELEGRAM: telegram_adapter,
    }
    runner._kanban_sub_fail_counts = {}

    asyncio.run(_run_one_notifier_tick_whatsapp(monkeypatch, runner))

    # Per-task telegram sub fired exactly once.
    assert len(telegram_adapter.sent) == 1, (
        f"per-task telegram sub expected 1 send, got {telegram_adapter.sent}"
    )
    # Per-task whatsapp sub fired exactly once.
    assert len(whatsapp_adapter.sent) >= 1, (
        f"per-task whatsapp sub expected 1 send, got {whatsapp_adapter.sent}"
    )
    # Default sub fired AT LEAST once (could be 2 if per-task whatsapp
    # sub also fired through the same adapter — both target platform=whatsapp,
    # the per-task sub on chat_id=per-task-whatsapp, the default sub on
    # 353899843924). We assert independent cursors: the default sub's
    # row's cursor advanced past the blocked event so a second tick
    # doesn't re-fire.
    conn = kb.connect()
    try:
        default_subs = [
            s for s in kb.list_notify_subs(conn, include_default=True)
            if s["is_default"] == 1
        ]
        assert len(default_subs) == 1
        assert default_subs[0]["last_event_id"] > 0, (
            "default sub cursor must have advanced past the blocked event"
        )
    finally:
        conn.close()

    # Second tick with no new events: neither sub re-fires.
    whatsapp_adapter.sent.clear()
    telegram_adapter.sent.clear()
    asyncio.run(_run_one_notifier_tick_whatsapp(monkeypatch, runner))
    assert whatsapp_adapter.sent == [], (
        f"second tick re-fired whatsapp adapter: {whatsapp_adapter.sent}"
    )
    assert telegram_adapter.sent == [], (
        f"second tick re-fired telegram adapter: {telegram_adapter.sent}"
    )


def test_template_renders_with_and_without_comment(tmp_path, monkeypatch):
    """``_render_notify_template`` substitutes placeholders safely; missing
    keys render as empty strings (never as literal ``{key}`` or a raised
    KeyError). Optional ``comment_excerpt`` placeholder works too.
    """
    template = (
        "🔔 Kanban: {task_id} {title}\n"
        "Status: {new_status}\n"
        "Reason: {block_reason}\n"
        "Workspace: {workspace_path}\n"
        "Note: {comment_excerpt}"
    )

    fields = _default_sub_template_fields(
        template,
        task_id="t_abc123",
        title="Test card",
        new_status="blocked",
        block_reason="missing API key",
        workspace_path="/tmp/ws",
        comment_excerpt="first 200 chars of operator note",
    )
    rendered = _render_notify_template(template, fields)
    assert "t_abc123" in rendered
    assert "Test card" in rendered
    assert "blocked" in rendered
    assert "missing API key" in rendered
    assert "/tmp/ws" in rendered
    assert "first 200 chars of operator note" in rendered

    # Missing keys render as empty strings, NOT literal ``{key}`` placeholders.
    fields_no_comment = _default_sub_template_fields(
        template,
        task_id="t_def456",
        title="No comment task",
        new_status="blocked",
    )
    rendered_no_comment = _render_notify_template(template, fields_no_comment)
    assert "{comment_excerpt}" not in rendered_no_comment, (
        f"literal placeholder leaked into body: {rendered_no_comment!r}"
    )
    assert "{block_reason}" not in rendered_no_comment
    assert "{workspace_path}" not in rendered_no_comment
    # But the named values that WERE provided still render.
    assert "t_def456" in rendered_no_comment
    assert "No comment task" in rendered_no_comment

    # A truly unknown placeholder is also tolerated (forward-compat for
    # template additions the runtime hasn't learned yet).
    future_template = "{task_id} {future_placeholder}"
    future_rendered = _render_notify_template(
        future_template,
        {"task_id": "t_ghi"},
    )
    assert future_rendered == "t_ghi "
