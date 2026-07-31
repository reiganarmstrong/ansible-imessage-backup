from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "recover_from_ios_backup.py"
SPEC = importlib.util.spec_from_file_location("recover_from_ios_backup", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def create_mac_export(root: Path) -> Path:
    export = root / "export"
    preservation = export / "Source Preservation"
    preservation.mkdir(parents=True)
    database = sqlite3.connect(preservation / "chat.db")
    database.executescript(
        """
        CREATE TABLE message (ROWID INTEGER PRIMARY KEY, guid TEXT);
        CREATE TABLE attachment (
            ROWID INTEGER PRIMARY KEY,
            transfer_name TEXT,
            total_bytes INTEGER,
            is_sticker INTEGER DEFAULT 0
        );
        CREATE TABLE message_attachment_join (
            message_id INTEGER,
            attachment_id INTEGER
        );
        INSERT INTO message VALUES (1, 'MESSAGE-GUID');
        INSERT INTO attachment VALUES (7, 'photo.heic', 11, 0);
        INSERT INTO message_attachment_join VALUES (1, 7);
        """
    )
    database.commit()
    database.close()
    (preservation / "attachment-manifest.json").write_text(
        json.dumps(
            [
                {
                    "row_id": 7,
                    "source_present": False,
                    "separately_recovered": False,
                    "hidden_internal_payload": False,
                }
            ]
        ),
        encoding="utf-8",
    )
    return export


def create_ios_backup(root: Path, payload: bytes) -> Path:
    backup = root / "ios-backup"
    backup.mkdir()
    sms_file_id = "3d" + "0" * 38
    attachment_file_id = "ab" + "1" * 38
    (backup / sms_file_id[:2]).mkdir()
    (backup / attachment_file_id[:2]).mkdir()

    sms = sqlite3.connect(backup / sms_file_id[:2] / sms_file_id)
    sms.executescript(
        """
        CREATE TABLE message (ROWID INTEGER PRIMARY KEY, guid TEXT);
        CREATE TABLE attachment (
            ROWID INTEGER PRIMARY KEY,
            transfer_name TEXT,
            total_bytes INTEGER,
            filename TEXT,
            hide_attachment INTEGER DEFAULT 0
        );
        CREATE TABLE message_attachment_join (
            message_id INTEGER,
            attachment_id INTEGER
        );
        INSERT INTO message VALUES (2, 'MESSAGE-GUID');
        INSERT INTO attachment VALUES (
            9, 'photo.heic', 11,
            '~/Library/SMS/Attachments/aa/01/UUID/photo.heic', 0
        );
        INSERT INTO message_attachment_join VALUES (2, 9);
        """
    )
    sms.commit()
    sms.close()
    (backup / attachment_file_id[:2] / attachment_file_id).write_bytes(payload)

    manifest = sqlite3.connect(backup / "Manifest.db")
    manifest.execute(
        "CREATE TABLE Files (fileID TEXT, domain TEXT, relativePath TEXT)"
    )
    manifest.executemany(
        "INSERT INTO Files VALUES (?, ?, ?)",
        [
            (sms_file_id, "HomeDomain", "Library/SMS/sms.db"),
            (
                attachment_file_id,
                "MediaDomain",
                "Library/SMS/Attachments/aa/01/UUID/photo.heic",
            ),
        ],
    )
    manifest.commit()
    manifest.close()
    return backup


class RecoverFromIosBackupTests(unittest.TestCase):
    def test_exact_same_message_attachment_is_copied_and_mapped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            export = create_mac_export(root)
            backup = create_ios_backup(root, b"hello world")
            destination = export / "Recovered Attachments" / "iOS Backup"
            source_map = (
                export / "Recovered Attachments" / "recovery-source-map.json"
            )

            report = MODULE.recover(
                export,
                backup,
                destination,
                source_map,
                include_device_variants=False,
                preserve_hidden_counterparts=False,
                apply=True,
            )

            self.assertEqual(report["byte_exact_candidate_count"], 1)
            self.assertEqual(report["device_variant_candidate_count"], 0)
            self.assertEqual(report["applied_count"], 1)
            mapped = json.loads(source_map.read_text(encoding="utf-8"))
            self.assertEqual(mapped["recoveries"][0]["attachment_row_ids"], [7])
            recovered = export / mapped["recoveries"][0]["archive_path"]
            self.assertEqual(recovered.read_bytes(), b"hello world")

    def test_variant_requires_explicit_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            export = create_mac_export(root)
            backup = create_ios_backup(root, b"hello world")
            ios_database_path = (
                backup / ("3d" + "0" * 38)[:2] / ("3d" + "0" * 38)
            )
            ios_database = sqlite3.connect(ios_database_path)
            ios_database.execute(
                "UPDATE attachment SET total_bytes = 12"
            )
            ios_database.commit()
            ios_database.close()
            attachment_path = (
                backup / ("ab" + "1" * 38)[:2] / ("ab" + "1" * 38)
            )
            attachment_path.write_bytes(b"hello world!")

            without_variants = MODULE.recover(
                export,
                backup,
                export / "Recovered Attachments" / "iOS Backup",
                export / "Recovered Attachments" / "recovery-source-map.json",
                include_device_variants=False,
                preserve_hidden_counterparts=False,
                apply=False,
            )
            with_variants = MODULE.recover(
                export,
                backup,
                export / "Recovered Attachments" / "iOS Backup",
                export / "Recovered Attachments" / "recovery-source-map.json",
                include_device_variants=True,
                preserve_hidden_counterparts=False,
                apply=False,
            )

            self.assertEqual(without_variants["candidate_count"], 0)
            self.assertEqual(with_variants["device_variant_candidate_count"], 1)


if __name__ == "__main__":
    unittest.main()
