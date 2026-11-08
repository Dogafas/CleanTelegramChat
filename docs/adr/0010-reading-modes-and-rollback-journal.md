# Reading modes, action plan, and safe rollback journal

## Status

Active

## Context

Unsubscribing is destructive and irreversible, especially for private or invite-only channels. Not every noisy channel justifies leaving: users often need low-friction alternatives (muting notifications, archiving, or organizing into a dedicated reading folder). Furthermore, multiple competing actions on the same channel can cause inconsistent states, and applying Telegram mutations without a safe rollback mechanism risks leaving the account in an unintended state.

## Decision

1. **Reversible Reading Modes vs. Irreversible Leave**:
   - Reversible alternatives: `mute` (forever mute notifications), `archive` (move to Telegram archive folder), and `folder` (add to dedicated reading folder without altering existing folder filters).
   - Unsubscribe (`leave`) is modelled as mutually exclusive with reading modes.
   - Channel action is modeled as the type union `Leave | Adjust`:
     - `Leave`: leaving the chat, cancels any active reading modes;
     - `Adjust(mute, archive, folder_title)`: reading modes are composable (e.g. archive + mute), and setting any mode automatically replaces a pending `Leave`.
     - State mutation is restricted to single-point methods on `ActionPlan`.

2. **Atomic and Safe Rollback Journal**:
   - Applied operations record prior state (`prev_state`) in a dedicated per-session rollback journal (`sessions/rollback_<session_name>.json`).
   - Atomic persistence: journal writes use a `.tmp` file and atomic replace to prevent corrupt files on crashes.
   - Repeated applications append new records to existing journal entries rather than overwriting them.
   - State-guard on rollback: before reverting `mute` or `archive`, the actual current state in Telegram is fetched. If the user modified the setting manually in Telegram after the script ran, the rollback is skipped (`skipped`) to protect user intent.
   - Consumption semantics: successfully restored and safely skipped entries are removed from the journal. Only entries that encountered errors remain in the file for retry.
   - Secrets, tokens, full post texts, and session credentials are never stored in the journal.
   - Unsubscribe operations are tracked as non-restorable.

3. **Non-Destructive Folder Integration (`chat_folder.py`)**:
   - Unlike `sort_chats.py` which never modifies existing folders (ADR 0008), the reading mode action `add_peer` adds a channel to an existing Telegram folder while strictly preserving all existing `include_peers`, `exclude_peers`, `pinned_peers`, and title settings. Rollback (`remove_peer`) removes only the added peer.

4. **Local Overlap Detection (`overlap.py`)**:
   - Exact duplicate detection runs locally in $O(N)$ without vector databases or LLM embeddings.
   - Three inverted indices: normalized text (NFKC, preserving numbers and negations), forward origins (`forward_origin`), and extracted material URLs.
   - Duplicates are evaluated strictly within overlapping observation time windows, computing asymmetric overlap ratios.

5. **Layer and Seam Deepening**:
   - Core modules (`review`, `plan`, `overlap`, `dialogs`, `chat_folder`, `subs`, `purge`, `verdict`, `catalog`, `session`) remain strictly separated from CLI scripts (`test_layers.py`).
   - `Review` provides an opaque domain model for scanned channels, sample coverage (`SampleCoverage`), and candidate classification with typed `VerdictStatus` (`VALID`, `NO_TEXT`, `ERROR`).
