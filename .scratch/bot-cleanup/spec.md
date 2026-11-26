# Spec: Bot cleanup — чистка бот-диалогов

## Problem Statement

В аккаунте накопились диалоги с ботами, которые не приносят пользы и создают информационный шум: боты, переставшие работать; боты, использованные один раз при настройке; боты-рекламные витрины, шлющие рекламу без участия пользователя. Телеграм не умеет массово находить и останавливать такие боты, а сторонняя аналитика (BotStat, Graspil) — только для владельцев ботов и подписчику недоступна.

## Solution

Новый сценарий «чистка ботов»: сканирует все диалоги с ботами (включая архив, где живёт папка «Bots»), вычисляет фиксированные локальные сигналы (молчание, неиспользование, спам без участия, доля рекламы, удалён/забанен), опционально проверяет живость пробой `/start`, отдаёт тексты на смысловой вердикт Jev (критерий «рекламная витрина»), сортирует кандидатов RED/YELLOW/GRAY и после ручного подтверждения выполняет «Остановить» (блокировка, обратимо) и «Удалить диалог» (не восстановимо), фиксируя всё в журнале отката.

## User Stories

1. As a Telegram account owner, I want to see all my bot dialogs in one scan, so that I know the true scale of bot noise.
2. As the same owner, I want archived bots included in the scan, so that bots parked in my archive folder are not silently skipped.
3. As an owner tired of silence, I want bots that have not sent anything for 3+ months flagged, so that obviously dormant bots surface automatically.
4. As an owner, I want bots I never actually used (at most one outgoing message ever) flagged, so that one-shot setup bots don't linger.
5. As an owner, I want the unused check to count my messages over the full dialog history, so that a bot I used long ago is not falsely marked unused because of a small sample.
6. As an owner annoyed by spam, I want bots that sent multiple messages after my last reply flagged, so that one-way ad streams stand out.
7. As an owner, I want the share of ad-marked or link-carrying bot messages computed, so that advertising showcases are detected locally without any AI call.
8. As a safety-conscious owner, I want deleted or banned bots detected immediately, so that dead dialogs are offered for cleanup without any extra checks.
9. As an owner, I want scam/fake flags from Telegram surfaced on the bot profile, so that flagged bots get extra scrutiny.
10. As a skeptical owner, I want an opt-in deep check that sends `/start` and waits 30 seconds, so that "dead" verdicts are proven against the only reliable signal — the bot's actual response.
11. As a cautious owner, I want the deep check off by default, so that live bots don't receive messages unless I explicitly opt in.
12. As an owner, I want bot texts and its description judged by Jev for "advertising showcase" semantics, so that ad bots that evade local regexes still get caught.
13. As a trust-conscious owner, I want only texts passed to Jev (no numbers), so that the model judges meaning instead of validating metrics.
14. As an owner, I want candidates split into RED (stop+delete recommended, pre-checked), YELLOW (indirect signals, unchecked), GRAY (insufficient data, unchecked), so that my attention goes to the confident cases first.
15. As a cautious owner, I want manual checkbox review and explicit confirmation before any action, so that nothing happens without my decision.
16. As a regretful owner, I want "stop" to be reversible via the rollback journal, so that I can unblock a bot I wrongly stopped.
17. As a realistic owner, I want "delete dialog" recorded as non-restorable in the journal, so that I know up front it cannot be undone.
18. As a folder user, I want my chat folder filters untouched by the cleanup, so that folder structure stays exactly as I arranged it.
19. As a flood-prone user, I want all network actions wrapped in flood-retry, so that a bulk cleanup doesn't get the account throttled.
20. As a multi-session owner, I want the scenario to reuse my existing session selection, so that the cleanup runs against the account I choose.

## Implementation Decisions

- New core module for bot review: scan (reusing the existing dialogs-and-archive traversal, filtered to bot chats), per-bot profile (bio, scam/fake markers, deleted/banned detection), full-history pagination, fixed signals, and RED/YELLOW/GRAY candidate routing — mirroring the channel review module's structure.
- Signal thresholds are fixed constants, consistent with existing metric signals: silence ≥ 90 days; ≤ 1 outgoing message over full history; ≥ 3 incoming after the user's last outgoing; ad share ≥ 25% via the shared ad-marker regex plus URL entities.
- Jev is reused as is: bot name maps to `channel`, bio (≤ 500 chars) to `description`, bot texts to `posts`; state keys stay `channel` / `description` / `posts`; numbers never enter the state (ADR 0009 discipline).
- The liveness probe is a core function (send `/start`, wait 30 s for any incoming update), activated only by an opt-in CLI mode; all network calls go through the existing flood-retry helper.
- New bot actions in the plan module: `Stop` (block user; reversible, restorable in the rollback journal with the existing state-guard that skips if the user already unblocked manually) and `DeleteChat` (delete chat history; non-restorable, tracked like `leave`). Stop precedes delete; journal format and consumption semantics follow ADR 0010.
- Deleting a dialog removes the bot from the "Bots" folder as a Telegram side effect; folder filters themselves are never modified (ADR 0008 discipline).
- New CLI scenario for bot cleanup with RED→YELLOW→GRAY checkbox UI and explicit confirmation; the import-layer test registers the new core module and scenario.
- No third-party services and no owner-side data (MAU, bot server uptime) — rejected as owner-side-only (ADR 0011 Non-goals).

## Testing Decisions

- Good tests assert external behavior through one seam: a fake Kurigram client object with injectable dialog list, chat history, and raw invocations, passed into core functions. No tests read internal row keys or call private helpers.
- Tested modules: bot scan/signals/candidate routing (fake dialogs and histories), bot actions and journal rollback in the plan module (fake invocations: stop restorable with state-guard, delete non-restorable).
- Prior art: the channel review, subs, and plan test suites — same fake-client construction; the layer test enforces the core/CLI import discipline for the new modules.
- The probe is tested through the fake client with a controllable wait, asserting that silence after `/start` yields the dead verdict and a reply does not.

## Out of Scope

- Third-party analytics integrations (BotStat, Graspil) and any owner-side bot statistics.
- Configurable signal thresholds (fixed like the existing metric signals).
- Mutations of chat folder filters; cleanup of channels/groups (covered by existing scenarios); mass unblocking; behavioral testing of the CLI layer.

## Further Notes

- All bots in this account live in the archive inside the "Bots" folder — the scan must include archive dialogs.
- Decision record: ADR 0011 (bot cleanup: local signals, optional liveness probe, stop and delete via rollback journal).
