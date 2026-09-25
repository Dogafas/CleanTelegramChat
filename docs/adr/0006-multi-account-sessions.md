# Multi-account session management in dedicated directory

Supersedes the single-session constraint of ADR 0003 ("Both entrypoints use session name cleaner").

Telegram session files are stored in a dedicated `sessions/` directory in the project root. Any legacy session files (`*.session` and `*.session-journal`) located in the root directory are automatically migrated into `sessions/` on first access.

Interactive menu supports:
1. Choosing an existing session by name.
2. Creating a new named session.
3. Selecting "Все сессии" (`ALL_SESSIONS`) for sequential message purge across all configured accounts.
4. Deleting an existing session with mandatory confirmation.

Entrypoint `people.py` supports an optional positional argument `[SESSION_NAME]`, defaulting to `cleaner`.
Session files are permanently preserved between runs and are only deleted upon explicit user confirmation.
