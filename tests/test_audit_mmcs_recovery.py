from __future__ import annotations

import importlib.util
import json
import plistlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "audit_mmcs_recovery.py"
SPEC = importlib.util.spec_from_file_location("audit_mmcs_recovery", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class AuditMmcsRecoveryTests(unittest.TestCase):
    def test_reports_availability_without_emitting_private_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            export = Path(temporary) / "export"
            preservation = export / "Source Preservation"
            preservation.mkdir(parents=True)
            (preservation / "attachment-manifest.json").write_text(
                json.dumps(
                    [
                        {
                            "row_id": 7,
                            "transfer_name": "photo.heic",
                            "database_total_bytes": 100,
                            "message_guids": ["MESSAGE-GUID"],
                            "hidden_internal_payload": False,
                            "source_present": False,
                            "separately_recovered": False,
                            "source_filename": (
                                "/var/folders/a/T/com.apple.imagent/"
                                "TemporaryItems/photo.heic"
                            ),
                        }
                    ]
                ),
                encoding="utf-8",
            )
            private_url = "https://p01-content.icloud.com/private-token"
            private_owner = "private-owner"
            private_key = "00" + "11" * 32
            metadata = plistlib.dumps(
                {
                    "mmcs-url": private_url,
                    "mmcs-owner": private_owner,
                    "mmcs-signature-hex": "81" + "22" * 20,
                    "decryption-key": private_key,
                    "file-size": 80,
                },
                fmt=plistlib.FMT_BINARY,
            )
            database = sqlite3.connect(preservation / "chat.db")
            database.execute(
                "CREATE TABLE attachment (ROWID INTEGER PRIMARY KEY, user_info BLOB)"
            )
            database.execute(
                "INSERT INTO attachment VALUES (7, ?)",
                (metadata,),
            )
            database.commit()
            database.close()

            report = MODULE.audit(export)
            serialized = json.dumps(report)

            self.assertEqual(report["rows_with_complete_download_metadata"], 1)
            self.assertEqual(report["complete_visible_rows"], 1)
            self.assertEqual(
                report["source_path_class_counts"],
                {"imagent_temporary": 1},
            )
            self.assertEqual(
                report["rows"][0]["source_path_class"],
                "imagent_temporary",
            )
            self.assertFalse(report["network_recovery_performed"])
            self.assertEqual(
                report["authenticated_messages_recovered_row_count"], 0
            )
            self.assertEqual(
                report["authenticated_messages_recovered_rows"], []
            )
            self.assertFalse(
                report["messages_ui_recovery_attempt_report_present"]
            )
            self.assertFalse(report["messages_ui_recovery_attempted"])
            self.assertTrue(report["requires_authenticated_messages_service"])
            self.assertEqual(report["rows"][0]["server_host"], "p01-content.icloud.com")
            self.assertNotIn(private_url, serialized)
            self.assertNotIn(private_owner, serialized)
            self.assertNotIn(private_key, serialized)

    def test_reports_authenticated_messages_recoveries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            export = Path(temporary) / "export"
            preservation = export / "Source Preservation"
            preservation.mkdir(parents=True)
            (preservation / "attachment-manifest.json").write_text(
                json.dumps(
                    [
                        {
                            "row_id": 7,
                            "source_present": False,
                            "separately_recovered": True,
                        }
                    ]
                ),
                encoding="utf-8",
            )
            database = sqlite3.connect(preservation / "chat.db")
            database.execute(
                "CREATE TABLE attachment (ROWID INTEGER PRIMARY KEY, user_info BLOB)"
            )
            database.commit()
            database.close()
            recovered = export / "Recovered Attachments"
            recovered.mkdir()
            (recovered / "recovery-source-map.json").write_text(
                json.dumps(
                    {
                        "recoveries": [
                            {
                                "attachment_row_ids": [7, 7],
                                "archive_path": (
                                    "Recovered Attachments/Apple MMCS/photo.heic"
                                ),
                                "sha256": "00" * 32,
                                "recovery_kind": (
                                    "messages_authenticated_download"
                                ),
                            },
                            {
                                "attachment_row_ids": [8],
                                "archive_path": (
                                    "Recovered Attachments/iOS/photo.heic"
                                ),
                                "sha256": "11" * 32,
                                "recovery_kind": "ios_backup_byte_exact",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            report = MODULE.audit(export)

            self.assertTrue(report["network_recovery_performed"])
            self.assertEqual(
                report["authenticated_messages_recovered_row_count"], 1
            )
            self.assertEqual(
                report["authenticated_messages_recovered_rows"], [7]
            )
            self.assertFalse(report["requires_authenticated_messages_service"])

    def test_summarizes_redacted_messages_ui_attempt_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            export = Path(temporary) / "export"
            preservation = export / "Source Preservation"
            preservation.mkdir(parents=True)
            (preservation / "attachment-manifest.json").write_text(
                "[]", encoding="utf-8"
            )
            database = sqlite3.connect(preservation / "chat.db")
            database.execute(
                "CREATE TABLE attachment (ROWID INTEGER PRIMARY KEY, user_info BLOB)"
            )
            database.commit()
            database.close()
            (export / "authenticated-messages-recovery-attempt.json").write_text(
                json.dumps(
                    {
                        "private_values_redacted": True,
                        "attempts": [
                            {
                                "attachment_row_id": 7,
                                "ui_attempted": True,
                                "result": "no_file_created",
                            },
                            {
                                "attachment_row_id": 8,
                                "ui_attempted": False,
                                "result": "no_chat_association",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            report = MODULE.audit(export)

            self.assertTrue(
                report["messages_ui_recovery_attempt_report_present"]
            )
            self.assertTrue(report["messages_ui_recovery_attempted"])
            self.assertEqual(
                report["messages_ui_recovery_attempted_rows"], [7]
            )
            self.assertEqual(
                report["messages_ui_recovery_not_attemptable_rows"], [8]
            )
            self.assertEqual(
                report["messages_ui_recovery_result_counts"],
                {"no_chat_association": 1, "no_file_created": 1},
            )


if __name__ == "__main__":
    unittest.main()
