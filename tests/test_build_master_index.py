from __future__ import annotations

import importlib.util
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "build_master_index.py"
SPEC = importlib.util.spec_from_file_location("build_master_index", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def create_archive(
    root: Path,
    name: str,
    *,
    text: str | None,
    rendered_text: str,
    source_present: bool,
    remote_verified: bool,
) -> Path:
    archive = root / name
    preservation = archive / "Source Preservation"
    preservation.mkdir(parents=True)
    (archive / "verification.json").write_text(
        json.dumps(
            {
                "passed": True,
                "source_attachment_coverage": {
                    "attachment_rows": 1,
                    "effectively_unrecovered_rows": (
                        0 if source_present else 1
                    ),
                },
            }
        ),
        encoding="utf-8",
    )
    (preservation / "attachment-manifest.json").write_text(
        json.dumps(
            [
                {
                    "row_id": 1,
                    "message_guids": ["MESSAGE-GUID"],
                    "source_present": source_present,
                    "separately_recovered": False,
                    "archive_path": (
                        "attachments/photo.jpg" if source_present else None
                    ),
                    "sha256": "digest" if source_present else None,
                }
            ]
        ),
        encoding="utf-8",
    )
    (archive / "Test Chat.html").write_text(
        f"""
        <html><body><div class="message"><div class="received">
        <a href="sms://open?message-guid=MESSAGE-GUID">Jan 1, 2024</a>
        <span class="sender">person@example.com</span>
        <div class="message_part"><span>{rendered_text}</span></div>
        </div></div></body></html>
        """,
        encoding="utf-8",
    )
    database = sqlite3.connect(preservation / "chat.db")
    database.executescript(
        """
        CREATE TABLE message (
            ROWID INTEGER PRIMARY KEY,
            guid TEXT NOT NULL,
            text TEXT,
            subject TEXT,
            date INTEGER,
            is_from_me INTEGER,
            handle_id INTEGER
        );
        CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
        CREATE TABLE chat (
            ROWID INTEGER PRIMARY KEY,
            guid TEXT,
            display_name TEXT,
            chat_identifier TEXT
        );
        CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
        """
    )
    database.execute(
        "INSERT INTO handle VALUES (1, 'person@example.com')"
    )
    database.execute(
        "INSERT INTO chat VALUES (1, 'CHAT-GUID', 'Test Chat', 'test')"
    )
    database.execute(
        "INSERT INTO message VALUES (1, 'MESSAGE-GUID', ?, NULL, 725846400000000000, 0, 1)",
        (text,),
    )
    database.execute("INSERT INTO chat_message_join VALUES (1, 1)")
    database.commit()
    database.close()
    if remote_verified:
        report_directory = (
            root
            / ".imessage-archive-state"
            / "remote-verifications"
        )
        report_directory.mkdir(parents=True, exist_ok=True)
        (report_directory / f"{name}.json").write_text(
            json.dumps(
                {
                    "passed": True,
                    "archive_name": name,
                    "verification_mode": "download",
                    "remote_destination": f"fake:remote/{name}",
                    "verified_at": "2024-01-10T00:00:00Z",
                    "file_count": 4,
                    "total_bytes": 100,
                }
            ),
            encoding="utf-8",
        )
    return archive


class BuildMasterIndexTests(unittest.TestCase):
    def test_indexes_verified_remote_catalog_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            name = "imessage-backup-20240101T000000"
            archive = create_archive(
                root,
                name,
                text="Cached database text",
                rendered_text="Cached rendered text",
                source_present=True,
                remote_verified=True,
            )
            cache = (
                root
                / ".imessage-archive-state"
                / "catalog-cache"
                / name
            )
            cache.parent.mkdir(parents=True)
            shutil.move(archive, cache)
            remote_marker = json.loads(
                (
                    root
                    / ".imessage-archive-state"
                    / "remote-verifications"
                    / f"{name}.json"
                ).read_text(encoding="utf-8")
            )
            (cache / ".catalog-cache-verification.json").write_text(
                json.dumps(
                    {
                        "passed": True,
                        "archive_name": name,
                        "remote_destination": remote_marker[
                            "remote_destination"
                        ],
                        "remote_verified_at": remote_marker["verified_at"],
                        "remote_file_count": remote_marker["file_count"],
                        "remote_total_bytes": remote_marker["total_bytes"],
                    }
                ),
                encoding="utf-8",
            )

            catalog, message_index = MODULE.build_master_index(
                root,
                "imessage-backup",
                ".imessage-archive-state",
            )

            self.assertEqual(catalog["archive_count"], 1)
            self.assertEqual(catalog["remote_verified_archive_count"], 1)
            self.assertEqual(
                catalog["archives"][0]["catalog_source"],
                "remote_cache",
            )
            self.assertEqual(message_index["unique_message_count"], 1)

    def test_ignores_unverified_remote_catalog_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            name = "imessage-backup-20240101T000000"
            archive = create_archive(
                root,
                name,
                text="Untrusted cache",
                rendered_text="Untrusted cache",
                source_present=True,
                remote_verified=True,
            )
            cache = (
                root
                / ".imessage-archive-state"
                / "catalog-cache"
                / name
            )
            cache.parent.mkdir(parents=True)
            shutil.move(archive, cache)

            catalog, _ = MODULE.build_master_index(
                root,
                "imessage-backup",
                ".imessage-archive-state",
            )

            self.assertEqual(catalog["archive_count"], 0)

    def test_complete_copy_wins_over_remote_incomplete_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            create_archive(
                root,
                "imessage-backup-20240101T000000",
                text="Database text",
                rendered_text="Rendered old text",
                source_present=True,
                remote_verified=False,
            )
            create_archive(
                root,
                "imessage-backup-20240108T000000",
                text="Database text",
                rendered_text="Rendered new text",
                source_present=False,
                remote_verified=True,
            )

            catalog, message_index = MODULE.build_master_index(
                root,
                "imessage-backup",
                ".imessage-archive-state",
            )

            self.assertEqual(catalog["archive_count"], 2)
            self.assertEqual(catalog["remote_verified_archive_count"], 1)
            self.assertEqual(catalog["unique_message_count"], 1)
            message = dict(
                zip(
                    message_index["message_fields"],
                    message_index["messages"][0],
                )
            )
            copies = [
                dict(zip(message_index["copy_fields"], copy))
                for copy in message["copies"]
            ]
            preferred = copies[message["preferred_copy_index"]]
            self.assertEqual(len(copies), 2)
            self.assertEqual(
                catalog["archives"][preferred["archive_index"]]["name"],
                "imessage-backup-20240101T000000",
            )
            self.assertEqual(
                sum(
                    copy["attachment_complete"]
                    and catalog["archives"][copy["archive_index"]][
                        "remote_verified"
                    ]
                    for copy in copies
                ),
                0,
            )

    def test_uses_rendered_html_for_rich_message_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            create_archive(
                root,
                "imessage-backup-20240101T000000",
                text=None,
                rendered_text="Rich rendered content",
                source_present=True,
                remote_verified=False,
            )

            _, message_index = MODULE.build_master_index(
                root,
                "imessage-backup",
                ".imessage-archive-state",
            )

            message = dict(
                zip(
                    message_index["message_fields"],
                    message_index["messages"][0],
                )
            )
            self.assertIn("Rich rendered content", message["text"])


if __name__ == "__main__":
    unittest.main()
