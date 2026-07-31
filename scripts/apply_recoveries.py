#!/usr/bin/env python3
"""Reconnect separately recovered files to exported conversation HTML."""

from __future__ import annotations

import html
import json
import mimetypes
import os
import sys
from pathlib import Path
from urllib.parse import quote


def attachment_markup(relative_path: str, filename: str) -> str:
    """Build conservative HTML for a recovered attachment."""
    encoded_path = quote(relative_path, safe="/")
    escaped_path = html.escape(encoded_path, quote=True)
    escaped_name = html.escape(filename)
    media_type, _ = mimetypes.guess_type(filename)

    if media_type and media_type.startswith("image/"):
        return (
            f'<div class="attachment"><a href="{escaped_path}">'
            f'<img src="{escaped_path}" alt="{escaped_name}"></a></div>'
        )
    if media_type and media_type.startswith("video/"):
        return (
            '<div class="attachment"><video controls>'
            f'<source src="{escaped_path}" type="{media_type}">'
            f'<source src="{escaped_path}">'
            "</video></div>"
        )
    if media_type and media_type.startswith("audio/"):
        return (
            '<div class="attachment"><audio controls>'
            f'<source src="{escaped_path}" type="{media_type}">'
            f'<source src="{escaped_path}">'
            "</audio></div>"
        )
    return (
        '<div class="attachment">'
        f'<a href="{escaped_path}" download>{escaped_name}</a>'
        "</div>"
    )


def apply_recovery(
    export_path: Path,
    recovered_assets_path: Path,
    recovery: dict[str, str],
) -> dict[str, str]:
    """Apply one manifest entry and return its result."""
    required_keys = {"message_guid", "missing_filename", "recovered_file"}
    missing_keys = required_keys - recovery.keys()
    if missing_keys:
        raise ValueError(
            f"Recovery entry is missing keys: {', '.join(sorted(missing_keys))}"
        )

    message_guid = recovery["message_guid"]
    missing_filename = recovery["missing_filename"]
    recovered_file = recovery["recovered_file"]
    recovered_path = recovered_assets_path / recovered_file
    if not recovered_path.is_file():
        raise FileNotFoundError(f"Recovered file does not exist: {recovered_path}")

    guid_marker = f"sms://open?message-guid={message_guid}"
    error_marker = (
        '<span class="attachment_error">Unable to locate attachment: '
        f"{html.escape(missing_filename)}</span>"
    )

    for html_file in sorted(export_path.glob("*.html")):
        document = html_file.read_text(encoding="utf-8", errors="strict")
        guid_position = document.find(guid_marker)
        if guid_position < 0:
            continue

        message_start = document.rfind('<div class="message">', 0, guid_position)
        next_message = document.find('<div class="message">', guid_position)
        message_end = len(document) if next_message < 0 else next_message
        if message_start < 0:
            raise ValueError(
                f"Could not locate the message container for GUID {message_guid}"
            )

        message_document = document[message_start:message_end]
        error_position = message_document.find(error_marker)
        relative_path = os.path.relpath(recovered_path, html_file.parent)
        markup = attachment_markup(relative_path, recovered_file)

        if error_position >= 0:
            updated_message = message_document.replace(error_marker, markup, 1)
            updated_document = (
                document[:message_start]
                + updated_message
                + document[message_end:]
            )
            html_file.write_text(updated_document, encoding="utf-8")
            return {
                "message_guid": message_guid,
                "missing_filename": missing_filename,
                "recovered_file": recovered_file,
                "conversation_html": html_file.name,
                "status": "patched",
            }

        if quote(relative_path, safe="/") in message_document:
            return {
                "message_guid": message_guid,
                "missing_filename": missing_filename,
                "recovered_file": recovered_file,
                "conversation_html": html_file.name,
                "status": "already_applied",
            }

        raise ValueError(
            f"Message {message_guid} does not contain the expected missing "
            f"attachment marker for {missing_filename}"
        )

    raise ValueError(f"No exported conversation contains message GUID {message_guid}")


def main() -> int:
    if len(sys.argv) != 4:
        print(
            "Usage: apply_recoveries.py EXPORT_PATH RECOVERED_ASSETS_PATH MANIFEST",
            file=sys.stderr,
        )
        return 2

    export_path = Path(sys.argv[1]).expanduser().resolve()
    recovered_assets_path = Path(sys.argv[2]).expanduser().resolve()
    manifest_path = Path(sys.argv[3]).expanduser().resolve()

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    recoveries = manifest.get("recoveries")
    if not isinstance(recoveries, list):
        raise ValueError("Manifest must contain a 'recoveries' list")

    results = [
        apply_recovery(export_path, recovered_assets_path, recovery)
        for recovery in recoveries
    ]
    report = {
        "manifest": str(manifest_path),
        "recovery_count": len(results),
        "patched_count": sum(result["status"] == "patched" for result in results),
        "already_applied_count": sum(
            result["status"] == "already_applied" for result in results
        ),
        "results": results,
    }
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
