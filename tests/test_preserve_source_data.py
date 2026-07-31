from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "preserve_source_data.py"
SPEC = importlib.util.spec_from_file_location("preserve_source_data", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def create_messages_database(path: Path, source_directory: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE message (
            ROWID INTEGER PRIMARY KEY,
            guid TEXT NOT NULL,
            text TEXT,
            subject TEXT,
            item_type INTEGER DEFAULT 0,
            associated_message_type INTEGER DEFAULT 0,
            date INTEGER DEFAULT 0,
            group_title TEXT,
            balloon_bundle_id TEXT
        );
        CREATE TABLE attachment (
            ROWID INTEGER PRIMARY KEY,
            guid TEXT NOT NULL,
            filename TEXT,
            transfer_name TEXT,
            total_bytes INTEGER DEFAULT 0,
            mime_type TEXT,
            uti TEXT,
            is_sticker INTEGER DEFAULT 0,
            hide_attachment INTEGER DEFAULT 0
        );
        CREATE TABLE message_attachment_join (
            message_id INTEGER,
            attachment_id INTEGER
        );
        CREATE TABLE chat (
            ROWID INTEGER PRIMARY KEY,
            display_name TEXT,
            chat_identifier TEXT
        );
        CREATE TABLE chat_message_join (
            chat_id INTEGER,
            message_id INTEGER
        );
        """
    )
    visible = source_directory / "IMG_2709.jpeg"
    hidden = source_directory / "payload.pluginPayloadAttachment"
    visible.write_bytes(b"\xff\xd8\xffvisible-image")
    hidden.write_bytes(b"internal-payload")
    connection.executemany(
        """
        INSERT INTO message (
            ROWID, guid, text, subject, item_type, associated_message_type,
            date, group_title, balloon_bundle_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (1, "MESSAGE-GUID", "hello", None, 0, 0, 1, None, None),
            (2, "OMITTED-GUID", None, None, 3, 0, 2, "Old group", None),
        ],
    )
    connection.executemany(
        """
        INSERT INTO attachment (
            ROWID, guid, filename, transfer_name, total_bytes, mime_type, uti,
            is_sticker, hide_attachment
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                1,
                "ATTACHMENT-1",
                str(visible),
                "IMG_2709.jpeg",
                visible.stat().st_size,
                "image/jpeg",
                "public.jpeg",
                0,
                0,
            ),
            (
                2,
                "ATTACHMENT-2",
                str(hidden),
                hidden.name,
                hidden.stat().st_size,
                None,
                "dynamic.payload",
                0,
                1,
            ),
            (
                3,
                "ATTACHMENT-3",
                str(source_directory / "missing.mov"),
                "missing.mov",
                100,
                "video/quicktime",
                "com.apple.quicktime-movie",
                0,
                0,
            ),
        ],
    )
    connection.executemany(
        "INSERT INTO message_attachment_join VALUES (?, ?)",
        [(1, 1), (1, 2), (2, 3)],
    )
    connection.execute(
        "INSERT INTO chat (ROWID, display_name, chat_identifier) VALUES (1, ?, ?)",
        ("Test Chat", "test@example.com"),
    )
    connection.executemany(
        "INSERT INTO chat_message_join VALUES (?, ?)",
        [(1, 1), (1, 2)],
    )
    connection.commit()
    connection.close()


class PreserveSourceDataTests(unittest.TestCase):
    def test_preserves_all_readable_rows_and_repairs_html(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            export_path = root / "export"
            source_path = root / "source"
            export_path.mkdir()
            source_path.mkdir()
            database = root / "chat.db"
            create_messages_database(database, source_path)
            conversation = export_path / "Test Chat.html"
            conversation.write_text(
                """
                <html><body>
                <div class="message">
                  <a href="sms://open?message-guid=MESSAGE-GUID">Date</a>
                  <span class="attachment_error">Unable to locate attachment: IMG_2709.PNG.jpeg</span>
                </div>
                </body></html>
                """,
                encoding="utf-8",
            )

            report, passed = MODULE.preserve(
                export_path,
                database,
                "Source Preservation",
                patch_html=True,
            )

            self.assertTrue(passed)
            self.assertEqual(report["summary"]["attachment_rows"], 3)
            self.assertEqual(report["summary"]["locally_readable_rows"], 2)
            self.assertEqual(
                report["summary"]["locally_readable_rows_preserved"],
                2,
            )
            self.assertEqual(
                report["summary"]["locally_readable_rows_unpreserved"],
                0,
            )
            self.assertEqual(report["summary"]["unavailable_rows"], 1)
            self.assertEqual(
                report["summary"]["standalone_messages_not_rendered"],
                1,
            )

            updated = conversation.read_text(encoding="utf-8")
            self.assertIn("source-preservation:1", updated)
            self.assertNotIn("attachment_error", updated)
            self.assertNotIn("source-preservation:2", updated)
            self.assertTrue(updated.strip().endswith("</html>"))

            preservation = export_path / "Source Preservation"
            self.assertTrue((preservation / "chat.db").is_file())
            self.assertTrue((preservation / "index.html").is_file())
            self.assertTrue(
                (preservation / "messages-not-rendered.html").is_file()
            )
            self.assertTrue(
                (preservation / "unavailable-attachments.html").is_file()
            )
            manifest = json.loads(
                (preservation / "attachment-manifest.json").read_text()
            )
            self.assertTrue(all(row["archive_path"] for row in manifest[:2]))
            self.assertIsNone(manifest[2]["archive_path"])

            snapshot = sqlite3.connect(preservation / "chat.db")
            self.assertEqual(
                snapshot.execute("SELECT count(*) FROM message").fetchone()[0],
                2,
            )
            snapshot.close()

    def test_second_pass_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            export_path = root / "export"
            source_path = root / "source"
            export_path.mkdir()
            source_path.mkdir()
            database = root / "chat.db"
            create_messages_database(database, source_path)
            conversation = export_path / "Test Chat.html"
            conversation.write_text(
                """
                <html><body>
                <div class="message">
                  <a href="sms://open?message-guid=MESSAGE-GUID">Date</a>
                </div>
                </body></html>
                """,
                encoding="utf-8",
            )

            MODULE.preserve(
                export_path,
                database,
                "Source Preservation",
                patch_html=True,
            )
            first_document = conversation.read_text(encoding="utf-8")
            report, passed = MODULE.preserve(
                export_path,
                database,
                "Source Preservation",
                patch_html=True,
            )
            second_document = conversation.read_text(encoding="utf-8")

            self.assertTrue(passed)
            self.assertEqual(first_document, second_document)
            self.assertTrue(second_document.strip().endswith("</html>"))
            self.assertEqual(
                report["summary"]["supplemental_unique_files_copied"],
                0,
            )

    def test_recovered_source_map_repairs_broken_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            export_path = root / "export"
            source_path = root / "source"
            recovered_path = export_path / "Recovered Attachments"
            export_path.mkdir()
            source_path.mkdir()
            recovered_path.mkdir()
            database = root / "chat.db"
            create_messages_database(database, source_path)
            database_connection = sqlite3.connect(database)
            database_connection.execute(
                """
                UPDATE attachment
                SET filename = ?, transfer_name = ?, total_bytes = ?
                WHERE ROWID = 1
                """,
                (
                    str(source_path / "missing-sticker.png.heic"),
                    "missing-sticker.png.heic",
                    len(b"recovered-sticker"),
                ),
            )
            database_connection.commit()
            database_connection.close()
            recovered = recovered_path / "missing-sticker.png.heic"
            recovered.write_bytes(b"recovered-sticker")
            digest = MODULE.DigestCache().sha256(recovered)
            recovery_map = recovered_path / "recovery-source-map.json"
            recovery_map.write_text(
                json.dumps(
                    {
                        "recoveries": [
                            {
                                "attachment_row_ids": [1],
                                "archive_path": (
                                    "Recovered Attachments/"
                                    "missing-sticker.png.heic"
                                ),
                                "sha256": digest,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            conversation = export_path / "Test Chat.html"
            conversation.write_text(
                """
                <html><body>
                <div class="message">
                  <a href="sms://open?message-guid=MESSAGE-GUID">Date</a>
                  <img src="missing-sticker.png.heic">
                </div>
                </body></html>
                """,
                encoding="utf-8",
            )

            report, passed = MODULE.preserve(
                export_path,
                database,
                "Source Preservation",
                patch_html=True,
                recovered_source_map=recovery_map,
            )

            self.assertTrue(passed)
            self.assertEqual(
                report["summary"]["html_link_status_counts"][
                    "repaired_broken_reference"
                ],
                1,
            )
            updated = conversation.read_text(encoding="utf-8")
            self.assertIn(
                "Recovered%20Attachments/missing-sticker.png.heic",
                updated,
            )
            self.assertNotIn('src="missing-sticker.png.heic"', updated)

    def test_converted_preview_counts_as_same_message_asset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            export_path = root / "export"
            source_path = root / "source"
            export_path.mkdir()
            source_path.mkdir()
            database = root / "chat.db"
            create_messages_database(database, source_path)
            heic = source_path / "photo.heic"
            heic.write_bytes(b"\x00\x00\x00\x18ftypheic-source")
            connection = sqlite3.connect(database)
            connection.execute(
                """
                UPDATE attachment
                SET filename=?, transfer_name=?, total_bytes=?,
                    mime_type='image/heic', uti='public.heic'
                WHERE ROWID=1
                """,
                (str(heic), heic.name, heic.stat().st_size),
            )
            connection.commit()
            connection.close()
            attachments = export_path / "attachments" / "1"
            attachments.mkdir(parents=True)
            (attachments / "1.heic").write_bytes(heic.read_bytes())
            (attachments / "1.jpeg").write_bytes(b"\xff\xd8\xffpreview")
            conversation = export_path / "Test Chat.html"
            conversation.write_text(
                """
                <html><body>
                <div class="message">
                  <a href="sms://open?message-guid=MESSAGE-GUID">Date</a>
                  <img src="attachments/1/1.jpeg">
                </div>
                </body></html>
                """,
                encoding="utf-8",
            )

            report, passed = MODULE.preserve(
                export_path,
                database,
                "Source Preservation",
                patch_html=True,
            )

            self.assertTrue(passed)
            self.assertNotIn(
                "source-preservation:",
                conversation.read_text(encoding="utf-8"),
            )
            self.assertEqual(
                report["summary"]["html_link_status_counts"]["already_linked"],
                1,
            )

    def test_separately_recovered_rows_reduce_effective_unavailable_count(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            export_path = root / "export"
            source_path = root / "source"
            recovered_path = export_path / "Recovered Attachments"
            export_path.mkdir()
            source_path.mkdir()
            recovered_path.mkdir()
            database = root / "chat.db"
            create_messages_database(database, source_path)
            (export_path / "Test Chat.html").write_text(
                """
                <html><body>
                <div class="message">
                  <a href="sms://open?message-guid=MESSAGE-GUID">Date</a>
                </div>
                </body></html>
                """,
                encoding="utf-8",
            )
            recovered = recovered_path / "missing.mov"
            recovered.write_bytes(b"recovered-video")
            import hashlib

            recovery_map = recovered_path / "recovery-source-map.json"
            recovery_map.write_text(
                json.dumps(
                    {
                        "recoveries": [
                            {
                                "attachment_row_ids": [3],
                                "archive_path": (
                                    "Recovered Attachments/missing.mov"
                                ),
                                "sha256": hashlib.sha256(
                                    b"recovered-video"
                                ).hexdigest(),
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            report, passed = MODULE.preserve(
                export_path,
                database,
                "Source Preservation",
                patch_html=True,
                recovered_source_map=recovery_map,
            )

            self.assertTrue(passed)
            self.assertEqual(report["summary"]["unavailable_rows"], 1)
            self.assertEqual(report["summary"]["separately_recovered_rows"], 1)
            self.assertEqual(report["summary"]["effectively_unrecovered_rows"], 0)
            manifest = json.loads(
                (
                    export_path
                    / "Source Preservation"
                    / "attachment-manifest.json"
                ).read_text()
            )
            self.assertTrue(manifest[2]["separately_recovered"])
            self.assertEqual(manifest[2]["coverage"], "separately_recovered")


if __name__ == "__main__":
    unittest.main()
