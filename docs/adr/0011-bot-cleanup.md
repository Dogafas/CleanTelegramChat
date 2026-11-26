# Bot cleanup: local signals, optional liveness probe, stop and delete via rollback journal

## Status

Active

## Context

Bot dialogs accumulate noise: bots that stopped working long ago, bots used once at setup, and bots that behave as advertising showcases (unsolicited ad streams without any user interaction). Third-party bot analytics services (BotStat, Graspil) were evaluated and rejected: both are owner-side analytics requiring the bot's token or an owner integration; BotStat's public catalog covers only bots registered with the service (an ad-heavy biased subset) and exposes no API for arbitrary third-party bots. The subscriber-side data source is the account's own Telegram dialogs.

All of the user's bots live in the archive inside the "Bots" folder; the scan must cover archived dialogs. Decisions approved by the user: Jev evaluates bots (Q1), actions are stop + delete chat (Q2), the `/start` liveness probe ships as an opt-in mode (Q3), signal thresholds are fixed like `metric_signals` (Q4), archive is scanned (Q5).

## Decision

1. **Scan (`bots.py`, reuses `dialogs.list_dialogs`)**:
   - Bot dialogs = dialogs (main + archive) with `Chat.type == ChatType.BOT`.
   - Profile per bot via `get_chat`: `Chat.bio` (description text), `is_scam` / `is_fake`. `get_users` error or deleted account → immediate candidate, no further checks.
   - All network calls go through `purge.retry_on_flood`.

2. **Full dialog history, not a sample**:
   - Bot dialog history is paginated via `get_chat_history` until exhausted; bot dialogs are short, pagination is cheap.
   - Outgoing messages are counted over the full history. A legacy-active bot later flooded with ads must not classify as unused.

3. **Fixed local signals** (thresholds fixed like `subs.metric_signals`, no config):
   - `молчит ≥ 3 мес.` — no messages in the dialog for ≥ 90 days (`Dialog.top_message.date` / history window);
   - `не использован` — outgoing ≤ 1 over full history (only `/start` or nothing);
   - `спам без участия` — ≥ 3 incoming bot messages after our last outgoing;
   - `рекламная витрина` — ≥ 25% of bot messages carrying `AD_MARKERS` (shared regex from `subs.py`) or `URL` / `TEXT_LINK` entities (same 25% bar as channel ad share);
   - `удалён / забанен` — profile fetch fails or account deleted.

4. **Opt-in liveness probe (default off)**:
   - Separate "глубокая проверка" mode: send `/start`, wait 30 s for any incoming update in that dialog; silence → `не отвечает на /start`.
   - The only definitive dead-bot test; cost is one message sent to every live bot (welcome spam possible). Flood-limits handled by `retry_on_flood`.

5. **Jev evaluates bots, text only (ADR 0009 rules)**:
   - `evaluate` is reused as is: bot name maps to `channel`, `Chat.bio` (≤ 500 chars) to `description`, bot message texts to `posts` (truncated like channel posts). State keys stay `channel` / `description` / `posts`; numbers are never passed in state.
   - Primary criterion `ads` (рекламная витрина); `slop`, `repetition`, `voice == noise` apply where bot texts allow.

6. **Candidate routing (ADR 0009 semantics)**:
   - **RED «Остановить и удалить»**: `удалён / забанен`, or `не отвечает на /start`, or Jev `ads` together with `не использован`. Checked by default.
   - **YELLOW «Проверить»**: indirect signals only (`молчит`, `спам без участия`, `рекламная витрина` locally). Unchecked by default.
   - **GRAY «Недостаточно данных»**: no usable texts or verdict error. Unchecked by default.
   - Manual checkbox changes and explicit confirmation remain mandatory before any mutation.

7. **Actions and rollback (`plan.py` + `journal.py`)**:
   - `Stop` = `block_user` — Telegram's native "stop bot". Reversible via `unblock_user`; restorable in the journal with the ADR 0010 state-guard (user unblocked manually → skip).
   - `DeleteChat` = `delete_chat_history` — removes the dialog. Non-restorable, tracked like `leave`.
   - `Stop` precedes `DeleteChat` when both are selected. Deleting the dialog removes the bot from the "Bots" folder as a Telegram side effect; folder filters themselves are never modified (ADR 0008 discipline).
   - Journal persistence, atomic writes, and consumption semantics follow ADR 0010. The journal lives in `journal.py` (records, state-guards, undo); `plan.py` builds and applies the plan.

8. **Layers**:
   - Core module `bots.py` (scan, signals, candidates); CLI scenario `clean_my_bots.py` (like `clean_my_subs.py`).
   - Both registered in `tests/test_layers.py`: core does not import CLI, scenarios do not import each other.

## Non-goals

- No third-party services: owner-side analytics (BotStat, Graspil) are unusable for a subscriber.
- Owner-side data (MAU, bot server uptime) is not obtainable from the Telegram API; server uptime is only provable via the `/start` probe.

## Sources

- `dialogs.py` (`list_dialogs`, `is_bot_dialog`), `subs.py` (`AD_MARKERS`, `metric_signals`), `review.py` (candidate routing), `plan.py` (ActionPlan, apply), `journal.py` (журнал отката, rollback), `purge.py` (`retry_on_flood`), `verdict.py` (`evaluate`);
- Kurigram: `Dialog.top_message`, `ChatType.BOT`, `Chat.bio`, `get_chat_history`, `block_user` / `unblock_user`, `delete_chat_history`;
- `docs/adr/0009-text-fail-unsubscribe.md`, `docs/adr/0010-reading-modes-and-rollback-journal.md`, `docs/channel-telegram-signals.md`.
