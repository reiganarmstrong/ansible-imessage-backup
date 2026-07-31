# Contributing

Contributions are welcome.

1. Do not add real Messages databases, exports, attachments, diagnostics, logs,
   private paths, or contact information.
2. Run `make check`.
3. Explain behavior and security implications in the pull request.
4. Keep tasks idempotent unless a task represents a new immutable export.

Tests must use synthetic message and attachment data only. Remote lifecycle
tests must use the bundled local rclone stand-in and must never contact a real
cloud account.
