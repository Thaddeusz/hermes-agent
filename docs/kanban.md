# Kanban

The Hermes kanban is the cross-agent task board. A small SQLite database
(defaults to `~/.hermes/kanban.db`) backs every card, comment, and event.
The dispatcher (in the gateway process, or as a one-shot CLI tick) claims
`ready` cards, spawns workers, and watches the system surface human-input
needs.

This document is the user-facing entry point. For exhaustive pitfall coverage
(quoting traps, parent-link chicken-and-eggs, review-required framing, etc.)
see `~/.hermes/profiles/main_profile/skills/hermes/hermes-kanban-usage/SKILL.md`.
For the multi-gateway deployment shape (one dispatch-owning gateway, others
with `dispatch_in_gateway: false`) see [`docs/kanban/multi-gateway.md`](kanban/multi-gateway.md).

## Card lifecycle

A card moves through a small set of statuses. Every transition is recorded
in the events log and surfaced via `hermes kanban show <id>`.

```
                            ┌──── skip_to_decompose (timeout) ────┐
                            ▼                                     │
triage ─▶ awaiting_clarification ──(hermes kanban triage --answer)──▶ triage ─▶ … ─▶ todo ─▶ ready ─▶ running ─▶ done
                                                              ▲              ▲                ▲                   ├▶ blocked (manual)
                                                              │              │                │                   ├▶ scheduled (time)
                                                              │              └─ recompute_ready() auto-promote     ├▶ review (review-required)
                                                              │                 (parent-done triggers)            └▶ archived
                                                              │
                                                  leave_in_awaiting (timeout, no-op)
```

| Status | What it means |
|---|---|
| **`triage`** | Raw / unscoped ideas. The dispatcher auto-runs the `kanban_decomposer` aux here. With `triage_clarify.enabled: true`, the watcher may park a card in `awaiting_clarification` before the decomposer sees it. |
| **`awaiting_clarification`** | A `triage` card the specifier parked because it needs human input (missing credentials, ambiguous scope, paywalled source). Carries questions in `clarification_questions`. Auto-decomposer skips it; dispatcher won't claim it. |
| **`todo`** | Spec is fleshed out. Waiting on **parent dependencies** OR **manual `hermes kanban promote <id>`**. |
| **`ready`** | Spec + unblocked. Dispatcher claims and spawns a worker. |
| **`running`** | Worker spawned, claim held. |
| **`blocked`** | Human-decision gate (precondition, review-required, operator-hold). |
| **`review`** | Shipped-and-awaiting-human-eyes (sub-state of `blocked`; semantically "your call"). |
| **`scheduled`** | Time-gated; promotes to `ready` when the time arrives. |
| **`done`** | Terminal. `hermes kanban show <id>` and the audit trail persist. |
| **`archived`** | Terminal. Out of sight. |

The `blocked` and `review` statuses live in the same code path (both go
through `unblock`); the distinction is in the *reason* the dispatcher
recorded. Cards in `review` need a thumbs-up from the user before
continuing.

## Interactive triage (awaiting_clarification)

The interactive-triage feature adds a clarifying-questions step before the
auto-decomposer fans a card out. With `kanban.triage_clarify.enabled: true`,
the watcher re-reads every dispatcher tick and may park a card in
`awaiting_clarification`. The CLI is the canonical answer interface for v1.

### End-to-end flow

1. **Triage card lands.** `hermes kanban create --triage "Title" --body "..."`.
2. **Watcher tick.** If `kanban.triage_clarify.enabled = true`, the
   `triage_clarify_watcher` (wired into `_kanban_dispatcher_watcher`) sweeps
   triage cards without `clarification_questions`. For each, it calls the
   question generator.
3. **Two outcomes from the generator:**
   - **2-3 questions returned:** the watcher stores them as JSON in
     `clarification_questions`, sets `clarification_asked_at = now`, flips
     `status = 'awaiting_clarification'`, atomically writes
     `~/.hermes/pending_clarifications.json` (temp + rename + fsync), and
     best-effort delivers to WhatsApp if connected.
   - **`[]` returned:** the card is treated as "already specific enough"
     and the auto-decomposer picks it up on the next tick as if the feature
     were disabled.
4. **User answers.** `hermes kanban triage --answer <TASK_ID> -q q1='...'` —
   see the CLI reference below for the full surface.
5. **Fold + resume.** `fold_clarification_answers` writes
   `clarification_answers` (JSON), appends `## User clarifications` to the
   body (idempotent on re-fold), nulls `clarification_questions`, and flips
   status back to `triage`. The auto-decomposer picks the card up on the
   next tick.
6. **Timeout.** If no answer arrives within
   `kanban.triage_clarify.timeout_days` (default 7), the watcher applies
   `on_timeout`: `skip_to_decompose` (default) clears
   `clarification_questions` and flips to `triage`; `leave_in_awaiting` is
   a no-op and the card stays parked.

### Master toggle

`kanban.triage_clarify.enabled` defaults to `false` (opt-in). The watcher
re-reads config every tick, so flipping the flag in `~/.hermes/config.yaml`
takes effect on the next dispatcher tick without a gateway restart. Flip to
`true` after the operator has reviewed and merged the relevant commits.

### Backwards compatibility

Pre-feature triage cards (created before the schema migration shipped, or
on boards where the feature shipped disabled) have no
`clarification_questions` column to populate — they flow straight through
to the auto-decomposer as if the feature were disabled. The
`kanban.triage_clarify.*` config block defaults to `enabled: false`, so
boards that don't opt in see no behavior change. To re-enable after a
rollout, set `enabled: true` in `~/.hermes/config.yaml`; no DB migration
is needed.

## CLI quick-reference

The full set of user-facing commands is `hermes kanban <subcommand>`. The
most relevant ones for the interactive-triage flow:

```bash
# Cards
hermes kanban list --status awaiting_clarification   # parked cards
hermes kanban list --status blocked                  # cards needing human input
hermes kanban list --status triage                   # cards awaiting decomposition
hermes kanban show <id>                              # full body + events + comments
hermes kanban show <id> --json                       # machine-readable for scripts

# Triage clarification (v1 answer interface)
hermes kanban triage --answer <TASK_ID> -q q1='...' -q q2='...'   # inline answers
hermes kanban triage --answer <TASK_ID> --answer-file path.json   # batched from file
hermes kanban triage --answer <TASK_ID> --stdin                  # batched from stdin
hermes kanban triage --answer <TASK_ID> --author "thaddeus"      # attribution on the audit comment

# Lifecycle transitions
hermes kanban promote <id> [--force]   # triage/todo → ready (with --force bypasses parent gate)
hermes kanban unblock <id> --reason "..."  # blocked/scheduled → todo (or ready if no parents)
hermes kanban archive <id>             # → archived
hermes kanban complete <id> --result "..."   # → done (terminal)

# Operator diagnostics
hermes kanban dispatch                 # force one dispatcher tick
hermes kanban tail <id>                # live event stream for a card
hermes kanban diag                     # active dispatcher diagnostics
hermes kanban stats                    # per-status counts + oldest-ready age
```

The `hermes kanban triage --answer` subcommand is the only v1 entry point
for clearing a parked card. v1 does NOT ship `--skip` / `--expire-now` /
`--list` aliases — use the recipes below instead:

- **List parked cards:** `hermes kanban list --status awaiting_clarification`
  (canonical) or `cat ~/.hermes/pending_clarifications.json` (raw, with the
  `asked_at` timestamp for staleness checks).
- **Force-expire early:** either answer with placeholder text so the fold
  releases it, or temporarily lower
  `kanban.triage_clarify.timeout_days` and wait for the next tick. If you
  need it on demand, file a follow-up card.

### Answer format

Questions are stored in the `clarification_questions` column as a JSON
array of `{id, question, why_we_ask}` objects. The answer id you pass via
`-q <id>=<answer>` (or the JSON `id` field) must match one of those
`id` values exactly. Sample `--answer-file` payload:

```json
[
  {"id": "q1", "answer": "Use the Stripe sandbox key in ~/.hermes/.env.stripe"},
  {"id": "q2", "answer": "Yes, scope to /v1/refunds only — no payouts"}
]
```

A `{answers: [...]}` wrapper is also accepted. Anything else in the file is
ignored. Unknown ids are dropped with a warning; missing answers for
known ids leave those questions unanswered (the card stays parked, just
with partial state — re-run with the missing ids to complete).

## Config block

`kanban.triage_clarify.*` (defaults in `hermes_cli/config.py`):

```yaml
kanban:
  triage_clarify:
    enabled: false               # master toggle; flip to true after reviewing the code
    max_questions: 3             # hard cap per card; watcher enforces defensively
    timeout_days: 7              # default days before a parked card times out
    on_timeout: skip_to_decompose   # or "leave_in_awaiting" for a no-op
    delivery:
      whatsapp:
        enabled: false           # best-effort WhatsApp delivery
        recipient: ""            # empty = gateway's default self-chat target
      cli:
        enabled: true            # /kanban show surfaces parked cards (no external deps)
```

| Key | Default | Effect |
|---|---|---|
| `kanban.triage_clarify.enabled` | `false` | Master toggle. When `false`, the specifier's question-generation path short-circuits to the existing "skip to decompose" behavior. Flipping the flag in `~/.hermes/config.yaml` takes effect on the next dispatcher tick without a gateway restart. |
| `kanban.triage_clarify.max_questions` | `3` | Hard cap on questions per task. The watcher enforces this defensively so a buggy specifier can't render an unbounded form. |
| `kanban.triage_clarify.timeout_days` | `7` | Default days before a parked card times out. Matches the cadence most operators want — long enough to notice a parked card on a slow week, short enough that a stale question doesn't accumulate forever. |
| `kanban.triage_clarify.on_timeout` | `skip_to_decompose` | What to do when the deadline expires without an answer. `skip_to_decompose` drops the questions and lets the decomposer proceed; `leave_in_awaiting` keeps the card parked. |
| `kanban.triage_clarify.delivery.whatsapp` | `enabled: false` | Best-effort WhatsApp delivery. `recipient: ""` falls through to the gateway's default self-chat target. |
| `kanban.triage_clarify.delivery.cli` | `enabled: true` | CLI delivery — `/kanban show` and the dashboard surface parked cards. Enabled by default because it has no external dependencies. |

For the rest of the dispatcher config (`dispatch_in_gateway`,
`auto_decompose_per_tick`, etc.) see the SKILL.md config table — those
require a `hermes gateway restart` after changes; `triage_clarify.*` does not.

## What's next

- **Review the umbrella and the relevant cards.** `t_7e2ad8b3` is the umbrella
  for the interactive-triage feature; children `t_a121beda` (schema),
  `t_92f8c0f0` (question generator), `t_e647d476` (watcher loop),
  `t_a6bc4b73` (answer CLI), and `t_7bd224b0` (E2E smoke) shipped the
  implementation. This document is the umbrella's acceptance bullet 10.
- **Flip `kanban.triage_clarify.enabled` to `true`** in `~/.hermes/config.yaml`
  when you're ready for the feature to actually fire. Cards will start
  parking in `awaiting_clarification` instead of flowing straight through
  to the decomposer.
- **Monitor `~/.hermes/pending_clarifications.json`** as the source of
  truth for parked questions — the file is written atomically (temp +
  rename + fsync) and is safe to read concurrently from cron jobs, status
  bars, or shell scripts.