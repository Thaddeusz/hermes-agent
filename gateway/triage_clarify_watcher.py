"""Triage clarification watcher (task t_e647d476).

Polls every board on a tick interval, sweeping for two populations of
triage-clarification work and acting on each:

  **Sweep A — fresh triage cards that need questions.** A task sitting
  in ``status='triage'`` with a NULL ``clarification_questions`` column
  is one the question generator has not yet visited on this board.
  The watcher calls ``hermes_cli.triage_clarify.generate_clarification_questions``
  on it, writes the JSON question payload back, parks the card in
  ``status='awaiting_clarification'`` with ``clarification_asked_at``
  set to now, and emits a delivery event so the next CLI session or
  WhatsApp subscriber surfaces the questions to the operator.

  **Sweep B — timed-out parked cards.** A card parked longer than its
  deadline (per-task override or global default) is acted on per the
  ``on_timeout`` policy: ``skip_to_decompose`` clears the questions and
  flips the card back to ``status='triage'`` so the existing
  auto-decomposer picks it up on its next tick; ``leave_in_awaiting``
  is a no-op (status stays so the operator sees "you owe me" at a
  glance).

Master toggle is ``kanban.triage_clarify.enabled`` in config.yaml —
default ``False`` so smoke testing is opt-in. Re-read from config
every tick (same safety-toggle pattern as ``_auto_decompose_tick``)
so flipping the flag takes effect on the next tick without a gateway
restart.

Designed to be invoked from the embedded dispatcher tick
(``gateway/kanban_watchers.py::GatewayKanbanWatchersMixin._kanban_dispatcher_watcher``)
once per ``dispatch_interval_seconds`` via ``asyncio.to_thread`` —
same shape as the auto-decompose tick it sits next to.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Optional, Tuple

logger = logging.getLogger("gateway.run")


# ---------------------------------------------------------------------------
# Settings resolution
# ---------------------------------------------------------------------------


def _resolve_triage_clarify_settings(
    load_config: Callable[[], Any],
) -> Tuple[bool, int, int, str, bool, str]:
    """Read ``kanban.triage_clarify`` fresh from config on every call.

    Returns ``(enabled, max_questions, timeout_days, on_timeout,
    whatsapp_enabled, whatsapp_recipient)``.

    The six return values mirror the six documented v1 keys, in the
    order they appear in the default block. Defaults match the
    defaults written to ``DEFAULT_CONFIG`` so an absent config block
    behaves identically to a partial one — call sites never have to
    think about which keys are set vs unset.

    Fails **safe**: any read or parse error returns a "disabled"
    tuple, so a transient config blip cannot accidentally enable
    clarification on a profile whose operator left the flag off.
    """
    try:
        cfg = load_config()
    except Exception:
        return False, 3, 7, "skip_to_decompose", False, ""

    kcfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    tc = kcfg.get("triage_clarify", {}) if isinstance(kcfg, dict) else {}
    if not isinstance(tc, dict):
        tc = {}

    enabled = bool(tc.get("enabled", False))

    # ``max_questions`` uses an ``in`` check so ``0`` is treated as
    # "user explicitly said 0" (clamped to 1 below) rather than
    # falling back to the default. ``auto_decompose_per_tick``
    # collapses 0 to the default because a 0 cap means "no
    # progress"; here 0 is the user's stated intent that they
    # want no questions asked (still clamped to 1, defensively).
    if "max_questions" in tc:
        try:
            max_questions = int(tc["max_questions"])
        except (TypeError, ValueError):
            max_questions = 3
    else:
        max_questions = 3
    if max_questions < 1:
        max_questions = 1

    if "timeout_days" in tc:
        try:
            timeout_days = int(tc["timeout_days"])
        except (TypeError, ValueError):
            timeout_days = 7
    else:
        timeout_days = 7
    if timeout_days < 1:
        timeout_days = 1

    on_timeout = tc.get("on_timeout", "skip_to_decompose")
    if on_timeout not in {"skip_to_decompose", "leave_in_awaiting"}:
        on_timeout = "skip_to_decompose"

    delivery = tc.get("delivery", {}) if isinstance(tc.get("delivery"), dict) else {}
    wa = delivery.get("whatsapp", {}) if isinstance(delivery.get("whatsapp"), dict) else {}
    whatsapp_enabled = bool(wa.get("enabled", False))
    whatsapp_recipient = str(wa.get("recipient", "") or "")

    return (
        enabled,
        max_questions,
        timeout_days,
        on_timeout,
        whatsapp_enabled,
        whatsapp_recipient,
    )


# ---------------------------------------------------------------------------
# Pending-clarifications file
# ---------------------------------------------------------------------------


def _pending_clarifications_path() -> Path:
    """Locate ``~/.hermes/pending_clarifications.json`` (operator-visible).

    Honours ``HERMES_HOME`` so test fixtures pointing at a temp
    ``HERMES_HOME`` exercise the real path resolution without touching
    the user's live directory.
    """
    home = Path(
        os.environ.get("HERMES_HOME")
        or os.environ.get("HERMES_PROFILE_HOME")
        or str(Path.home() / ".hermes")
    )
    return home / "pending_clarifications.json"


def _read_pending_clarifications() -> dict:
    """Return the current pending-clarifications file or an empty dict."""
    path = _pending_clarifications_path()
    try:
        if not path.exists():
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        # Corrupt file is recoverable: a fresh dict overwrite next tick
        # replaces it. Operators with a half-written file shouldn't lose
        # the whole feature to a stray crash.
        return {}


def _write_pending_clarifications(payload: dict) -> None:
    """Atomically write ``pending_clarifications.json`` (temp + rename).

    Atomic write via temp-file + ``os.replace`` so a process crash mid-write
    can't leave the operator with a half-written JSON file. ``fsync``
    on the temp file before rename flushes the kernel buffer so the
    rename is durable across power loss.
    """
    path = _pending_clarifications_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning(
            "triage_clarify: cannot create pending_clarifications dir (%s): %s",
            path.parent, exc,
        )
        return
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix="pending_clarifications.", suffix=".tmp",
            dir=str(path.parent),
        )
    except OSError as exc:
        logger.warning(
            "triage_clarify: cannot create temp file in %s: %s",
            path.parent, exc,
        )
        return
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(payload, indent=2, sort_keys=True))
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                # fsync is best-effort — some filesystems (e.g. tmpfs)
                # reject it. Atomic rename still gives crash-safety in
                # the common case.
                pass
        os.replace(tmp_name, path)
    except Exception as exc:
        logger.warning(
            "triage_clarify: failed to write %s: %s", path, exc,
        )
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Per-board helpers
# ---------------------------------------------------------------------------


def _list_triage_ids_needing_questions(conn) -> list:
    """Return ``id``s of triage cards without questions yet on this board.

    Scoped to ``status='triage' AND clarification_questions IS NULL`` —
    exactly the population the specifier hasn't visited yet. Once the
    watcher writes questions, the card leaves this set on the next tick.
    """
    rows = conn.execute(
        "SELECT id FROM tasks "
        "WHERE status = 'triage' AND clarification_questions IS NULL "
        "ORDER BY created_at ASC"
    ).fetchall()
    return [r["id"] for r in rows]


def _list_awaiting_with_age(conn, now_ts: int, timeout_days: int):
    """Return parked cards whose deadline has passed.

    Returns ``[(id, per_task_timeout_days_or_none), ...]`` so the caller
    can decide whether to flip each card back to triage. Excludes
    cards whose ``clarification_asked_at`` is NULL (defensive — a card
    with status awaiting but no ask timestamp means the writer crashed
    before finishing the row).
    """
    rows = conn.execute(
        "SELECT id, clarification_asked_at, clarification_timeout_days "
        "FROM tasks WHERE status = 'awaiting_clarification' "
        "AND clarification_asked_at IS NOT NULL"
    ).fetchall()
    out = []
    cutoff_seconds = timeout_days * 86400
    for r in rows:
        per_task = r["clarification_timeout_days"]
        try:
            per_task_days = int(per_task) if per_task is not None else timeout_days
        except (TypeError, ValueError):
            per_task_days = timeout_days
        if per_task_days < 1:
            per_task_days = timeout_days
        per_task_cutoff = per_task_days * 86400
        if now_ts - int(r["clarification_asked_at"]) >= per_task_cutoff:
            out.append((r["id"], per_task_days))
    return out


def _default_whatsapp_sender(recipient: str, message: str) -> bool:
    """No-op sender used when the gateway hasn't injected a real one.

    The watcher module is imported standalone (it has no implicit
    dependency on ``gateway.run`` being fully initialised), so the
    real WhatsApp adapter is wired in by the caller via the
    ``whatsapp_sender`` parameter on :func:`triage_clarify_tick`. When
    the caller passes ``None``, this fallback runs and WhatsApp
    delivery silently no-ops — the ``pending_clarifications.json``
    file is still the durable record so a missing sender doesn't lose
    the question.
    """
    return False


def _send_whatsapp_clarification(
    sender, recipient: str, title: str, questions: list
) -> bool:
    """Best-effort WhatsApp delivery via the injected sender.

    The caller (``GatewayKanbanWatchersMixin._kanban_dispatcher_watcher``)
    wires ``sender`` to a closure that knows how to route through the
    gateway's live adapter set. This keeps the watcher module
    independent of ``gateway.run`` internals so it can be unit-tested
    with a stub sender in isolation.

    Returns ``True`` if the message was sent, ``False`` on every
    failure path (no sender, no recipient, send exception). The
    pending_clarifications.json file is the durable copy regardless.
    """
    if sender is None:
        return False
    if not recipient:
        return False
    lines = [f"Clarification needed for: {title}", ""]
    for idx, q in enumerate(questions, 1):
        if not isinstance(q, dict):
            continue
        text = q.get("question") or q.get("id") or "?"
        lines.append(f"{idx}. {text}")
        why = q.get("why_we_ask")
        if why:
            lines.append(f"   ({why})")
    message = "\n".join(lines)
    try:
        return bool(sender(recipient, message))
    except Exception as exc:
        logger.debug(
            "triage_clarify: whatsapp delivery failed for %s: %s", recipient, exc,
        )
        return False


# ---------------------------------------------------------------------------
# Per-tick work
# ---------------------------------------------------------------------------


def _process_triage_card(
    conn, task_id: str, max_questions: int,
    whatsapp_enabled: bool, whatsapp_recipient: str,
    whatsapp_sender,
    pending: dict,
) -> Optional[dict]:
    """Generate questions for one triage card and park it.

    Returns the entry written to ``pending_clarifications`` on
    success (``{task_id, title, questions, asked_at}``) so the
    caller can fold it into the file write. Returns ``None`` if the
    card was skipped (zero questions, the model decided no
    clarification needed) or if any step raised — the caller logs
    and moves on.
    """
    from hermes_cli import kanban_db as _kb
    from hermes_cli import triage_clarify as _tc

    row = conn.execute(
        "SELECT id, title, body FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return None

    title = row["title"] or ""
    body = row["body"] or ""

    try:
        questions = _tc.generate_clarification_questions(
            title=title, body=body, max_questions=max_questions,
        )
    except _tc.TriageClarifyError as exc:
        logger.warning(
            "triage_clarify: question generation failed for %s (%s); leaving card in triage",
            task_id, exc,
        )
        return None
    except Exception as exc:
        # Auxiliary client unavailable, timeout, etc. — treat as
        # transient. Card stays in triage for the next tick to retry.
        logger.warning(
            "triage_clarify: question generation crashed for %s (%s); leaving card in triage",
            task_id, exc,
        )
        return None

    if not questions:
        # Zero is a valid model output (the spec decided no
        # clarification was needed). Card should NOT be parked — leave
        # it for the auto-decomposer to pick up on its next tick.
        logger.info(
            "triage_clarify: %s → 0 questions (model decided none needed); skipping",
            task_id,
        )
        return None

    # Defensive trim — the generator already caps to max_questions,
    # but if a future change loosens that contract the watcher must
    # still not surface an unbounded form.
    questions = questions[:max_questions]

    now = int(time.time())
    try:
        with _kb.write_txn(conn):
            cur = conn.execute(
                "UPDATE tasks SET status = 'awaiting_clarification', "
                "clarification_questions = ?, clarification_asked_at = ? "
                "WHERE id = ? AND status = 'triage' AND clarification_questions IS NULL",
                (json.dumps(questions), now, task_id),
            )
            if cur.rowcount == 0:
                # Lost a race with another watcher / specifier instance.
                return None
    except Exception as exc:
        logger.warning(
            "triage_clarify: DB write failed for %s (%s); card stays in triage",
            task_id, exc,
        )
        return None

    entry = {
        "task_id": task_id,
        "title": title,
        "questions": questions,
        "asked_at": now,
    }

    # Delivery: WhatsApp is best-effort. The pending file is the
    # durable record so a WhatsApp outage doesn't lose the question.
    if whatsapp_enabled:
        sent = _send_whatsapp_clarification(
            whatsapp_sender, whatsapp_recipient, title, questions,
        )
        if sent:
            logger.info(
                "triage_clarify: delivered %d question(s) for %s to whatsapp",
                len(questions), task_id,
            )
        else:
            logger.debug(
                "triage_clarify: whatsapp delivery unavailable for %s; "
                "pending_clarifications.json is the durable copy",
                task_id,
            )

    return entry


def _process_timed_out_card(
    conn, task_id: str, on_timeout: str,
) -> str:
    """Apply the configured timeout policy to one parked card.

    Returns a short status tag (``"decomposed"``, ``"left"``,
    ``"skipped"``) for the per-tick log line.
    """
    from hermes_cli import kanban_db as _kb

    if on_timeout == "leave_in_awaiting":
        return "left"

    # skip_to_decompose: clear questions, flip back to triage. The
    # existing auto-decomposer picks it up on its next tick with
    # the original body (we deliberately do not fold the question
    # payload into the body — the specifier's original write of the
    # body is the source of truth for what to decompose).
    try:
        with _kb.write_txn(conn):
            cur = conn.execute(
                "UPDATE tasks SET status = 'triage', "
                "clarification_questions = NULL, clarification_asked_at = NULL "
                "WHERE id = ? AND status = 'awaiting_clarification'",
                (task_id,),
            )
            if cur.rowcount == 0:
                return "skipped"
    except Exception as exc:
        logger.warning(
            "triage_clarify: timeout flip failed for %s (%s); leaving parked",
            task_id, exc,
        )
        return "skipped"

    return "decomposed"


def triage_clarify_tick(
    load_config: Callable[[], Any],
    list_boards: Callable[..., list],
    connect: Callable[..., Any],
    *,
    whatsapp_sender=None,
) -> dict:
    """One sweep across every board. Returns per-tick telemetry.

    Mirrors the shape of ``_auto_decompose_tick`` so the dispatcher
    can call it inside ``asyncio.to_thread`` exactly the same way.
    Per-board env pinning (``HERMES_KANBAN_BOARD``) keeps each
    kanban_db call resolved against the right DB without threading
    board kwargs through every helper — same trick the auto-decompose
    loop uses, since the kanban_db module connects via the env.

    ``whatsapp_sender`` is an optional ``Callable[[str, str], bool]``
    injected by the dispatcher. It receives ``(recipient, message)``
    and returns ``True`` on a successful send. When ``None`` (or
    ``whatsapp_enabled=False``), delivery silently no-ops — the
    ``pending_clarifications.json`` file is still the durable
    record so a missing sender doesn't lose the question.

    Returns ``{"asked": int, "timed_out": int, "boards_visited": int}``
    so the caller can log a one-line summary per tick without having
    to scrape logger output.
    """
    (
        enabled,
        max_questions,
        timeout_days,
        on_timeout,
        whatsapp_enabled,
        whatsapp_recipient,
    ) = _resolve_triage_clarify_settings(load_config)

    if not enabled:
        # Quiet by default — operators running with the flag off should
        # not see "0 boards processed" every tick.
        return {"asked": 0, "timed_out": 0, "boards_visited": 0}

    try:
        boards = list_boards(include_archived=False)
    except Exception as exc:
        logger.debug(
            "triage_clarify: list_boards failed (%s); skipping tick", exc,
        )
        return {"asked": 0, "timed_out": 0, "boards_visited": 0}

    now = int(time.time())
    pending = _read_pending_clarifications()
    asked = 0
    timed_out = 0
    boards_visited = 0
    pending_dirty = False

    for b in boards:
        slug = b.get("slug") or "default"
        prev_env = os.environ.get("HERMES_KANBAN_BOARD")
        try:
            os.environ["HERMES_KANBAN_BOARD"] = slug
            try:
                conn = connect(board=slug)
            except Exception as exc:
                logger.debug(
                    "triage_clarify: connect failed for board %s (%s); skipping",
                    slug, exc,
                )
                continue
            try:
                boards_visited += 1

                # Sweep A — fresh triage cards
                try:
                    triage_ids = _list_triage_ids_needing_questions(conn)
                except Exception as exc:
                    logger.debug(
                        "triage_clarify: triage query failed on %s (%s)",
                        slug, exc,
                    )
                    triage_ids = []

                for tid in triage_ids:
                    entry = _process_triage_card(
                        conn, tid, max_questions,
                        whatsapp_enabled, whatsapp_recipient,
                        whatsapp_sender, pending,
                    )
                    if entry is not None:
                        pending[entry["task_id"]] = entry
                        pending_dirty = True
                        asked += 1

                # Sweep B — timed-out parked cards
                try:
                    timed_out_rows = _list_awaiting_with_age(
                        conn, now, timeout_days,
                    )
                except Exception as exc:
                    logger.debug(
                        "triage_clarify: awaiting query failed on %s (%s)",
                        slug, exc,
                    )
                    timed_out_rows = []

                for tid, _per_task_days in timed_out_rows:
                    result = _process_timed_out_card(conn, tid, on_timeout)
                    if result == "decomposed":
                        # Card flipped back to triage — remove from
                        # pending file so the next CLI session doesn't
                        # surface a question that's no longer pending.
                        if tid in pending:
                            del pending[tid]
                            pending_dirty = True
                        timed_out += 1
                    elif result == "left":
                        # leave_in_awaiting — log once per tick per
                        # card, but do not modify pending or the DB.
                        logger.info(
                            "triage_clarify: %s past deadline; on_timeout=leave_in_awaiting",
                            tid,
                        )
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        finally:
            if prev_env is None:
                os.environ.pop("HERMES_KANBAN_BOARD", None)
            else:
                os.environ["HERMES_KANBAN_BOARD"] = prev_env

    if pending_dirty:
        _write_pending_clarifications(pending)

    return {"asked": asked, "timed_out": timed_out, "boards_visited": boards_visited}