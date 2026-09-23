Superseded for the supergroup-only filter and the stdin adapter by 0004.

# Chat Catalog is separate from the terminal

Supergroup filtering and 1-based selection parsing are a module. `input()` is only the CLI adapter. Tests call `parse_selection` and never stdin.
