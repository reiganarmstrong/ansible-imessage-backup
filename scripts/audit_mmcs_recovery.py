#!/usr/bin/env python3
"""Audit unavailable attachments for Apple MMCS recovery metadata.

The report deliberately records only availability, sizes, and server hosts. It
never emits the private download URL, owner token, signature, or decryption key.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import sqlite3
import sys
from pathlib import Path
from urllib.parse import urlsplit


PRIVATE_MMCS_FIELDS = (
    "mmcs-url",
    "mmcs-owner",
    "mmcs-signature-hex",
    "decryption-key",
)
AUTHENTICATED_MESSAGES_RECOVERY_KIND = "messages_authenticated_download"
MESSAGES_RECOVERY_ATTEMPT_REPORT = (
    "authenticated-messages-recovery-attempt.json"
)


def source_path_class(source_filename: object) -> str:
    if not isinstance(source_filename, str) or not source_filename:
        return "no_path"
    expanded = str(Path(source_filename).expanduser())
    if "/Library/Messages/Attachments/" in expanded:
        return "messages_attachments"
    if (
        expanded.startswith(("/var/folders/", "/private/var/folders/"))
        and "/com.apple.imagent/" in expanded
    ):
        return "imagent_temporary"
    return "other"


def authenticated_messages_recovered_rows(export_path: Path) -> list[int]:
    recovery_map_path = (
        export_path / "Recovered Attachments" / "recovery-source-map.json"
    )
    if not recovery_map_path.is_file():
        return []
    recovery_map = json.loads(recovery_map_path.read_text(encoding="utf-8"))
    if not isinstance(recovery_map, dict):
        raise ValueError("Recovery source map must be a JSON object")
    recoveries = recovery_map.get("recoveries", [])
    if not isinstance(recoveries, list):
        raise ValueError("Recovery source map recoveries must be a JSON array")

    recovered_rows: set[int] = set()
    for recovery in recoveries:
        if (
            not isinstance(recovery, dict)
            or recovery.get("recovery_kind")
            != AUTHENTICATED_MESSAGES_RECOVERY_KIND
        ):
            continue
        row_ids = recovery.get("attachment_row_ids", [])
        if not isinstance(row_ids, list):
            raise ValueError(
                "Recovery source map attachment_row_ids must be a JSON array"
            )
        recovered_rows.update(
            row_id for row_id in row_ids if isinstance(row_id, int)
        )
    return sorted(recovered_rows)


def messages_recovery_attempt_summary(
    export_path: Path,
) -> dict[str, object]:
    report_path = export_path / MESSAGES_RECOVERY_ATTEMPT_REPORT
    empty_summary: dict[str, object] = {
        "messages_ui_recovery_attempt_report_present": False,
        "messages_ui_recovery_attempted": False,
        "messages_ui_recovery_attempted_row_count": 0,
        "messages_ui_recovery_attempted_rows": [],
        "messages_ui_recovery_not_attemptable_row_count": 0,
        "messages_ui_recovery_not_attemptable_rows": [],
        "messages_ui_recovery_result_counts": {},
    }
    if not report_path.is_file():
        return empty_summary

    attempt_report = json.loads(report_path.read_text(encoding="utf-8"))
    if not isinstance(attempt_report, dict):
        raise ValueError(
            "Authenticated Messages recovery attempt report must be an object"
        )
    if attempt_report.get("private_values_redacted") is not True:
        raise ValueError(
            "Authenticated Messages recovery attempt report must confirm "
            "private value redaction"
        )
    attempts = attempt_report.get("attempts", [])
    if not isinstance(attempts, list):
        raise ValueError(
            "Authenticated Messages recovery attempts must be a JSON array"
        )

    attempted_rows: set[int] = set()
    not_attemptable_rows: set[int] = set()
    result_counts: dict[str, int] = {}
    for attempt in attempts:
        if not isinstance(attempt, dict):
            raise ValueError(
                "Authenticated Messages recovery attempt must be an object"
            )
        row_id = attempt.get("attachment_row_id")
        result = attempt.get("result")
        ui_attempted = attempt.get("ui_attempted")
        if (
            not isinstance(row_id, int)
            or not isinstance(result, str)
            or not isinstance(ui_attempted, bool)
        ):
            raise ValueError(
                "Authenticated Messages recovery attempts require an integer "
                "attachment_row_id, string result, and boolean ui_attempted"
            )
        result_counts[result] = result_counts.get(result, 0) + 1
        if ui_attempted:
            attempted_rows.add(row_id)
        else:
            not_attemptable_rows.add(row_id)

    attempted = sorted(attempted_rows)
    not_attemptable = sorted(not_attemptable_rows)
    return {
        "messages_ui_recovery_attempt_report_present": True,
        "messages_ui_recovery_attempted": bool(attempted),
        "messages_ui_recovery_attempted_row_count": len(attempted),
        "messages_ui_recovery_attempted_rows": attempted,
        "messages_ui_recovery_not_attemptable_row_count": len(
            not_attemptable
        ),
        "messages_ui_recovery_not_attemptable_rows": not_attemptable,
        "messages_ui_recovery_result_counts": dict(sorted(result_counts.items())),
    }


def audit(export_path: Path) -> dict[str, object]:
    preservation = export_path / "Source Preservation"
    manifest_path = preservation / "attachment-manifest.json"
    database_path = preservation / "chat.db"
    if not manifest_path.is_file() or not database_path.is_file():
        raise FileNotFoundError(
            "Source Preservation attachment manifest or chat.db is missing"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, list):
        raise ValueError("Attachment manifest must be a JSON array")
    unresolved = {
        row["row_id"]: row
        for row in manifest
        if (
            isinstance(row, dict)
            and isinstance(row.get("row_id"), int)
            and not row.get("source_present")
            and not row.get("separately_recovered")
        )
    }

    database = sqlite3.connect(
        f"file:{database_path}?mode=ro&immutable=1",
        uri=True,
    )
    rows: list[dict[str, object]] = []
    parse_error_rows: list[int] = []
    try:
        for row_id, user_info in database.execute(
            "SELECT ROWID, user_info FROM attachment WHERE user_info IS NOT NULL"
        ):
            manifest_row = unresolved.get(row_id)
            if manifest_row is None:
                continue
            try:
                metadata = plistlib.loads(user_info)
            except Exception:
                parse_error_rows.append(row_id)
                continue
            if not isinstance(metadata, dict):
                parse_error_rows.append(row_id)
                continue
            url = metadata.get("mmcs-url")
            host = (
                urlsplit(url).hostname
                if isinstance(url, str) and url
                else None
            )
            complete = all(metadata.get(field) for field in PRIVATE_MMCS_FIELDS)
            mmcs_size = metadata.get("file-size")
            rows.append(
                {
                    "row_id": row_id,
                    "transfer_name": manifest_row.get("transfer_name"),
                    "database_total_bytes": manifest_row.get(
                        "database_total_bytes"
                    ),
                    "mmcs_payload_bytes": (
                        mmcs_size if isinstance(mmcs_size, int) else None
                    ),
                    "message_guids": manifest_row.get("message_guids", []),
                    "hidden_internal_payload": bool(
                        manifest_row.get("hidden_internal_payload")
                    ),
                    "source_path_class": source_path_class(
                        manifest_row.get("source_filename")
                    ),
                    "server_host": host,
                    "has_complete_download_metadata": complete,
                    "metadata_fields_present": {
                        field: bool(metadata.get(field))
                        for field in (*PRIVATE_MMCS_FIELDS, "file-size")
                    },
                }
            )
    finally:
        database.close()

    complete_rows = [
        row for row in rows if row["has_complete_download_metadata"]
    ]
    path_class_counts: dict[str, int] = {}
    for row in rows:
        path_class = str(row["source_path_class"])
        path_class_counts[path_class] = (
            path_class_counts.get(path_class, 0) + 1
        )
    authenticated_recovered_rows = authenticated_messages_recovered_rows(
        export_path
    )
    report = {
        "export_path": str(export_path),
        "unresolved_attachment_rows": len(unresolved),
        "rows_with_parseable_mmcs_metadata": len(rows),
        "rows_with_complete_download_metadata": len(complete_rows),
        "source_path_class_counts": dict(sorted(path_class_counts.items())),
        "complete_visible_rows": sum(
            not row["hidden_internal_payload"] for row in complete_rows
        ),
        "complete_hidden_rows": sum(
            row["hidden_internal_payload"] for row in complete_rows
        ),
        "parse_error_count": len(parse_error_rows),
        "parse_error_rows": parse_error_rows,
        "private_values_redacted": True,
        "network_recovery_performed": bool(authenticated_recovered_rows),
        "authenticated_messages_recovered_row_count": len(
            authenticated_recovered_rows
        ),
        "authenticated_messages_recovered_rows": authenticated_recovered_rows,
        "requires_authenticated_messages_service": bool(complete_rows),
        "rows": rows,
    }
    report.update(messages_recovery_attempt_summary(export_path))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export_path", type=Path)
    parser.add_argument("--report-path", type=Path)
    args = parser.parse_args()
    export_path = args.export_path.expanduser().resolve()
    if not export_path.is_dir():
        parser.error(f"{export_path} is not a directory")
    report = audit(export_path)
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.report_path:
        destination = args.report_path.expanduser().resolve()
        destination.write_text(rendered, encoding="utf-8")
        os.chmod(destination, 0o600)
    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
