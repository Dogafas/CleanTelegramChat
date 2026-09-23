# Message Purge is one deep module

Message search, the 100-id delete batch, FloodWait retry, and human pauses live behind one purge interface. Callers pass a chat id and a Telegram client adapter. Chunk size, jitter, and retry stay inside the module so a future script cannot "simplify" them away at the call site.
