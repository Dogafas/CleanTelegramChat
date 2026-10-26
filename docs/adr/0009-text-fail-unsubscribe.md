# Text fail adds an unsubscribe candidate; Jev runs on every subscription

## Status

Active, with completed cutover to multi-tier candidate priority (RED / YELLOW / GRAY).

## Context & Decision

Jev evaluates every Channel Subscription with post text (up to 30 posts truncated to 1000 characters). Jev `state` remains TEXT ONLY (`channel`, `description`, `posts`); numeric metrics and scores are never passed to the model.

Text failures evaluated by Jev:
- `slop` (нейрослоп: формульный текст без ставки);
- `clickbait` (кликбейт: неоплаченное обещание в теле);
- `ads` (рекламная витрина: доминирование рекламы);
- `emotional_pressure` (эмоциональное давление: систематическая манипулятивная эксплуатация страха, стыда, зависти, возмущения или FOMO; неприятные правдивые новости сами по себе не являются давлением);
- `repetition` (повторы без новой информации; развитие темы и обновления не являются провалом);
- `voice == "noise"` (пустой поток).

Course selling alone is NOT an independent text failure (legitimate course sales are permissible; ad dominance, clickbait or manipulative pressure remain flagged under their respective criteria).

Candidate UI Routing:
1. **RED «Отписаться»**: Valid verdict with $\ge 1$ semantic text failures. Checked by default.
2. **YELLOW «Проверить»**: Valid verdict with no text failures, but indirect metric signals exist (e.g. frequency > 60 posts/month, low engagement). Unchecked by default.
3. **GRAY «Недостаточно данных»**: No usable semantic verdict (media-only / no text, API error, malformed output). Unchecked by default; takes precedence over metric colors, showing indirect signals as secondary reasons.
4. **Clean**: Valid verdict with no text failures and no indirect signals is excluded from candidates.

Sorting in checkbox UI: RED, then YELLOW, then GRAY.
Row contents: label + chat name + voice (if available) + concrete triggered reasons (no total scores and no misleading Noul percentages). Color is applied only to the short label. Manual checkbox changes and explicit user confirmation remain mandatory before any unsubscribe operation.
