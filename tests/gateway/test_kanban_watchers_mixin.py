"""Tests for the extracted GatewayKanbanWatchersMixin (god-file Phase 3).

The kanban watcher loops were lifted out of gateway/run.py into a mixin that
GatewayRunner inherits. These tests confirm the mixin exposes the methods and
that GatewayRunner picks them up via the MRO (behavior-neutral relocation).
"""

from __future__ import annotations

import inspect

from gateway.kanban_watchers import GatewayKanbanWatchersMixin

KANBAN_METHODS = [
    "_kanban_notifier_watcher",
    "_kanban_dispatcher_watcher",
    "_kanban_advance",
    "_kanban_unsub",
    "_kanban_rewind",
    "_deliver_kanban_artifacts",
]


def test_mixin_defines_kanban_methods():
    for m in KANBAN_METHODS:
        assert hasattr(GatewayKanbanWatchersMixin, m), f"mixin missing {m}"


def test_gateway_runner_inherits_mixin():
    # Import here so a heavy gateway import only happens if the first test passed.
    from gateway.run import GatewayRunner

    assert issubclass(GatewayRunner, GatewayKanbanWatchersMixin)
    # Each kanban method resolves to the mixin's implementation via the MRO.
    for m in KANBAN_METHODS:
        owner = next(c for c in GatewayRunner.__mro__ if m in c.__dict__)
        assert owner is GatewayKanbanWatchersMixin, (
            f"{m} resolved to {owner.__name__}, expected the mixin"
        )


def test_watcher_loops_are_coroutines():
    # The two long-running watchers are async loops.
    assert inspect.iscoroutinefunction(GatewayKanbanWatchersMixin._kanban_notifier_watcher)
    assert inspect.iscoroutinefunction(GatewayKanbanWatchersMixin._kanban_dispatcher_watcher)


def test_singleton_dispatcher_lock_is_exclusive(tmp_path):
    """Only one holder of the dispatcher lock at a time — the backstop that
    stops concurrent dispatchers double reclaiming and corrupting shared
    kanban SQLite index pages under wal_autocheckpoint=0."""
    import os

    from gateway.kanban_watchers import _acquire_singleton_lock, _release_singleton_lock

    lock = tmp_path / "kanban" / ".dispatcher.lock"

    h1, st1 = _acquire_singleton_lock(lock)
    assert st1 == "held" and h1 is not None

    # A second acquire while the first is held must be refused, not granted.
    h2, st2 = _acquire_singleton_lock(lock)
    assert st2 == "contended" and h2 is None

    # Releasing the first lets a fresh acquire succeed (lock is reusable).
    _release_singleton_lock(h1)
    h3, st3 = _acquire_singleton_lock(lock)
    assert st3 == "held" and h3 is not None
    _release_singleton_lock(h3)


def test_triage_clarify_runs_before_auto_decompose():
    """Regression test for the dispatcher-tick ordering bug.

    Observed 2026-06-22: triage card t_14f9193c decomposed 74s after entering
    triage because the dispatcher tick ran ``_auto_decompose_tick`` BEFORE
    ``triage_clarify_tick``. The auto-decomposer saw the fresh triage card and
    fanned it out before the watcher had a chance to park it in
    ``awaiting_clarification``. This test pins the source-code ordering so the
    race cannot silently regress.

    The test inspects the dispatcher loop's source (not runtime behavior)
    because the actual asyncio loop is hard to drive deterministically in a
    unit test, and the ordering is a static property of the source anyway.
    """
    import inspect

    from gateway.kanban_watchers import GatewayKanbanWatchersMixin

    src = inspect.getsource(GatewayKanbanWatchersMixin._kanban_dispatcher_watcher)

    # The dispatcher imports ``triage_clarify_tick`` locally (late-bound)
    # inside the tick body. We need the CALL site (not the import statement)
    # because runtime order is what matters. Both calls live inside
    # ``await asyncio.to_thread(<callable>, ...)`` blocks; the function
    # name is followed by a comma (positional arg), not an open paren.
    triage_clarify_pos = src.find("triage_clarify_tick,")
    auto_decompose_pos = src.find("_auto_decompose_tick,")

    assert triage_clarify_pos != -1, (
        "triage_clarify_tick call no longer present in the dispatcher loop "
        "— if it was moved, this test should be updated to match the new design"
    )
    assert auto_decompose_pos != -1, (
        "_auto_decompose_tick call no longer present in the dispatcher loop"
    )
    assert triage_clarify_pos < auto_decompose_pos, (
        "triage_clarify_tick must run BEFORE _auto_decompose_tick in the "
        "dispatcher loop, otherwise a fresh triage card lands in triage "
        "and is decomposed in the same tick before the watcher can park it. "
        f"Found triage_clarify at offset {triage_clarify_pos}, "
        f"auto_decompose at offset {auto_decompose_pos}."
    )
