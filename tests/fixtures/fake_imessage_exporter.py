#!/usr/bin/env python3
"""Synthetic imessage-exporter used for local playbook integration tests."""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    arguments = sys.argv[1:]

    if "--version" in arguments:
        print("imessage-exporter 0.0.0-test")
        return 0

    if "--diagnostics" in arguments:
        print("Synthetic diagnostics: no real Messages data was accessed.")
        return 0

    try:
        export_path = Path(arguments[arguments.index("--export-path") + 1])
    except (ValueError, IndexError):
        print("Missing --export-path", file=sys.stderr)
        return 2

    attachment_directory = export_path / "attachments"
    attachment_directory.mkdir(parents=True, exist_ok=True)
    (attachment_directory / "photo.jpg").write_bytes(b"synthetic image")
    if "--synthetic-missing" in arguments:
        conversation = """
        <html><body>
        <div class="message">
          <a href="sms://open?message-guid=TEST-GUID">Date</a>
          <span class="attachment_error">Unable to locate attachment: original.mov</span>
        </div>
        </body></html>
        """
    else:
        conversation = (
            '<html><body><img src="attachments/photo.jpg"></body></html>'
        )
    (export_path / "Synthetic Conversation.html").write_text(
        conversation,
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
