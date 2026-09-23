# One session module, session file kept

API id/hash resolve from `TELEGRAM_API_ID` + `TELEGRAM_API_HASH`, else `.env`, else a one-time read of `cache.json` that writes `.env`, else a prompt that writes `.env`. `cache.json` is not deleted. Resolution runs when a client is opened, never at import. Both entrypoints use session name `cleaner`. The process must not delete `*.session`. Deleting the session on exit forced a phone and 2FA login every run; keeping the file is the point of the module.
